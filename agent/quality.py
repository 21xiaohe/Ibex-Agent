"""Per-execution coding checks, separate from the frozen external evaluation suite.

The same configured model drafts requirements tests without seeing the generated
implementation. These are fallible internal checks, not a correctness oracle.
"""
import ast
import asyncio
import hashlib
import json
import re
import shlex
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools import tool


QUALITY_RULES = """
编码流程：先按原始需求逐条列出正常、边界、非法输入和副作用验收，再修改源码和测试。
不要只测试正常值。区分 bool 与严格整数；金额先判断 Decimal.is_finite() 再比较或计算，
显式把输入解析错误映射到需求指定的异常。API 使用安装版本，inspect_python_api 可检查签名。
工具返回的错误表示操作未成功；不要靠重复相同参数重试。edit_file 匹配失败先重新读取文件，
需要整体重写时用 replace_file，并提供读取时的内容 SHA256（可用 inspect_file 获得）。
改完调用 verify_changes：它运行从原始需求单独生成的契约测试和项目测试。根据真实失败修复，
不得删测试、跳过断言或削弱校验。测试通过后立即总结，避免无关重构。没有验证成功不得声称完成。
内部契约测试也可能有误，遇到与需求矛盾时明确报告阻塞；不能修改契约测试制造通过。
"""

CONTRACT_PROMPT = """You are a requirements-test author, separate from the implementation step.
Return ONLY a JSON object with requires_changes (bool), requirements (list of strings),
and tests (one executable pytest module string). Use only the ORIGINAL USER REQUEST and
the pre-change project context. Do not add requirements not specified by the user.
For an explanation, exact reply, read-only review, or file-only non-Python task return
requires_changes=false, requirements=[], tests="".
For Python implementation tasks: create runnable tests importing the requested public
module/function. Cover EVERY explicit contract: normal, empty, boundaries, exact output,
invalid inputs/exception types, no mutation and idempotency when requested. Include all
explicit invalid classes, not one representative. bool is an int subclass; strict int
contracts must reject bool. Nonfinite Decimal inputs require is_finite before comparisons.
Use decimal for money and compute expected values carefully. For FastAPI use TestClient
and the installed version, test invalid request status. Never assert implementation details.
No sleep/network/services; use injected mocks. Do not implement the solution. Tests must
be plain test_* functions with pytest assertions, without custom plugins, conftest,
skip/xfail, or fixtures other than pytest built-ins. At least 3 tests. Keep under 120 lines.
The implementation is not available yet. Do not import any eval module or hidden tests.
"""

CONTRACT_REVIEW_PROMPT = """Audit the proposed tests against the ORIGINAL USER REQUEST.
You have no implementation. Return ONLY JSON {"additional_tests": "...pytest Python...",
"coverage_notes": ["..."]}. Preserve existing tests: add missing cases, do not delete any.
Use unique test_coverage_* names and imports. Do not add constraints not in the request.
Check every invalid-input equivalence class, including malformed numeric text, NaN,
infinity, boolean-vs-int, fractional-vs-int, missing/empty input when relevant. Test exact
rounding boundaries and rounding once rather than per item if required. For stateful
operations test rollback/no mutation on failure; for APIs test status codes and strictness.
Do not assume a negative number test also covers a malformed string or NaN. Tests must
raise/assert the exact promised exception class, not broad Exception. Keep expected values
simple and verify their arithmetic. No sleeps, external services, skip, xfail or plugins.
If complete, additional_tests can be empty. Keep additions under 100 lines.
"""

REFERENCE_PROMPT = """Return ONLY JSON {"files":{"app/module.py":"Python source"}}.
Create a small reference implementation from the ORIGINAL REQUEST and initial project.
You do NOT see the candidate implementation or its generated tests. Honor exactly the
requested API, invalid input and rounding semantics. Use installed Python/FastAPI/Pydantic
v2 APIs. Decimal for money; finiteness before comparisons; reject bool for strict integer
contracts. All files must be Python under app/. No tests, external services or processes.
Use ordinary functions/classes; do not use Pydantic or FastAPI unless the original
request explicitly asks for an HTTP API. Preserve positional constructor signatures.
This code is solely an isolated internal-test preflight oracle, never shown to the agent.
"""


class QualityBlocked(RuntimeError):
    """Execution failed its bounded internal verification; never a success result."""


def parse_generated_json(text: str) -> dict:
    """Parse source strings containing literal newlines without evaluating their code.

    strict=False relaxes only the JSON string control-character restriction. Syntax,
    object type, path and AST checks remain mandatory; never use eval for repair.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    data = json.loads(text, strict=False)
    if not isinstance(data, dict):
        raise ValueError("Generated JSON must be an object")
    return data


def parse_contract(text: str) -> dict:
    """Reject malformed/empty test plans instead of silently allowing completion."""
    data = parse_generated_json(text)
    if type(data.get("requires_changes")) is not bool:
        raise ValueError("Missing contract classification")
    if not data["requires_changes"]:
        return {"requires_changes": False, "requirements": [], "tests": ""}
    tests = data.get("tests", "")
    if not isinstance(tests, str) or len(tests) > 24000:
        raise ValueError("Invalid contract size")
    tree = ast.parse(tests)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    if len(functions) < 3 or not data.get("requirements"):
        raise ValueError("Contract needs requirements and at least three tests")
    if re.search(r"\b(skip|skipif|xfail|pytest_plugins|conftest)\b", tests):
        raise ValueError("Contract cannot disable tests")
    return data


def python_command(source: str, workdir: str) -> str:
    """Quote generated commands without executing text in the host shell."""
    return f"cd {shlex.quote(workdir)} && python -c {shlex.quote(source)}"


class CodingQuality(AgentMiddleware):
    """Own one request's contract, bounded tool recovery and real verification gate.

    Instances belong to one graph execution scope, never shared across tenants.
    """

    def __init__(self, backend: Any, model_factory: Callable) -> None:
        self.backend = backend
        self.model_factory = model_factory
        self.tools = self._tools()
        self.original = ""
        self.contract: dict | None = None
        self.last_verification: dict | None = None
        self.events: list[dict] = []
        self.initial: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.tool_count = 0
        self.final_retries = 0
        self.failed_calls: dict[str, int] = {}

    async def abefore_agent(self, state: dict, runtime: Any) -> None:
        """Capture the actual user request, not later verification feedback."""
        self.original = next((str(m.content) for m in reversed(state["messages"]) if m.type == "human"), "")

    async def snapshot(self, include_context: bool = False) -> dict:
        """Hash project Python sources/tests and return bounded initial source context."""
        source = """import pathlib,hashlib,json
root=pathlib.Path('.')
files={}; context={}
for folder in ('app','tests'):
 for p in sorted((root/folder).rglob('*.py')):
  if '__pycache__' in p.parts or p.is_symlink(): continue
  raw=p.read_bytes()
  files[p.as_posix()]=hashlib.sha256(raw).hexdigest()
  if INCLUDE_CONTEXT and sum(len(v) for v in context.values())<2400: context[p.as_posix()]=raw.decode(errors='replace')[:800]
print(json.dumps({'files':files,'context':context}))
""".replace('INCLUDE_CONTEXT',repr(include_context))
        result = await self.backend.aexecute(python_command(source, self.backend.workdir), timeout=15)
        if result.exit_code:
            raise QualityBlocked("Cannot inspect project before verification")
        return json.loads(result.output)

    async def prepare(self) -> None:
        """Generate a frozen internal test contract before the first potentially mutating tool."""
        async with self.lock:
            if self.contract is not None:
                return
            before = await self.snapshot(include_context=True)
            self.initial = before["files"]
            from middleware.concurrency import _llm_sem
            async with _llm_sem:
                response = await self.model_factory().ainvoke([
                    {"role": "system", "content": CONTRACT_PROMPT},
                    {"role": "user", "content": json.dumps({"request": self.original, "initial_project": before["context"]}, ensure_ascii=False)}],
                    config={"tags": ["requirements-contract"], "callbacks": []})
            contract = parse_contract(str(response.content))
            if contract["requires_changes"]:
                async with _llm_sem:
                    review = await self.model_factory().ainvoke([
                        {"role":"system","content":CONTRACT_REVIEW_PROMPT},
                        {"role":"user","content":json.dumps({"request":self.original,"proposed_tests":contract["tests"]},ensure_ascii=False)}],
                        config={"tags":["contract-coverage-review"],"callbacks":[]})
                coverage = parse_generated_json(str(review.content))
                additional = coverage.get("additional_tests", "")
                if not isinstance(additional, str):
                    raise QualityBlocked("Invalid contract coverage review")
                contract = parse_contract(json.dumps({**contract, "tests": contract["tests"] + "\n" + additional}))
                self.events.append({"type":"contract_review","coverage_notes":coverage.get("coverage_notes",[]),
                                    "usage":getattr(review,"usage_metadata",None)})
            if contract["requires_changes"]:
                contract = await self.preflight_contract(contract, before["context"])
            self.contract = contract
            self.events.append({"type": "contract", "requirements": self.contract["requirements"],
                                "tests": self.contract["tests"], "usage": getattr(response, "usage_metadata", None)})

    async def preflight_contract(self, contract: dict, context: dict) -> dict:
        """Check generated tests against a spec-only reference before candidate edits."""
        from middleware.concurrency import _llm_sem
        async with _llm_sem:
            reference = await self.model_factory().ainvoke([
                {"role":"system","content":REFERENCE_PROMPT},
                {"role":"user","content":json.dumps({"request":self.original,"initial_project":context},ensure_ascii=False)}],
                config={"tags":["contract-reference"],"callbacks":[]})
        files = parse_generated_json(str(reference.content)).get("files")
        if not isinstance(files, dict) or not files or len(files)>10:
            raise QualityBlocked("Invalid reference files")
        for name, body in files.items():
            if not re.fullmatch(r"app/(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py",name) or not isinstance(body,str) or len(body)>24000:
                raise QualityBlocked("Invalid reference path or content")
        self.events.append({"type":"reference_usage","usage":getattr(reference,"usage_metadata",None)})
        original_count = len([n for n in ast.parse(contract["tests"]).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')])
        for attempt in range(2):
            source = """import pathlib,tempfile,json,subprocess,sys,os,shutil
with tempfile.TemporaryDirectory(prefix='ibex-oracle-') as folder:
 root=pathlib.Path(folder)
 shutil.copytree('app',root/'app',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
 for name,body in FILES.items():
  p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body,encoding='utf-8')
 test=root/'test_contract.py';test.write_text(TESTS,encoding='utf-8')
 env=dict(os.environ,PYTHONPATH=folder,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
 result=subprocess.run([sys.executable,'-m','pytest','-q','-c',os.devnull,str(test)],cwd=folder,env=env,capture_output=True,text=True,timeout=40)
 print(json.dumps({'passed':result.returncode==0,'output':(result.stdout+result.stderr)[-12000:]}))
""".replace('FILES',repr(files)).replace('TESTS',repr(contract['tests']))
            checked = await self.backend.aexecute(python_command(source,self.backend.workdir),timeout=50)
            try:
                result=json.loads(checked.output.strip().splitlines()[-1])
            except (ValueError,IndexError):
                result={'passed':False,'output':checked.output[-12000:]}
            self.events.append({'type':'contract_preflight','attempt':attempt+1,'passed':bool(result.get('passed')),
                                'output':result.get('output',''),'tests':contract['tests']})
            if result.get('passed') and checked.exit_code==0:
                return contract
            if attempt:
                raise QualityBlocked('Contract/reference disagreement before implementation')
            async with _llm_sem:
                corrected=await self.model_factory().ainvoke([
                    {'role':'system','content':"Reconcile tests against ORIGINAL REQUIREMENTS and reference code. Return ONLY JSON {tests: full pytest module, files: complete app reference files, reason: string}. Correct only demonstrated mistakes; never weaken or delete coverage. Check arithmetic with Decimal expressions rather than mental calculation. No access to candidate implementation. Keep at least the original number of test functions. If the spec cannot resolve the conflict, return unchanged and explain it."},
                    {'role':'user','content':json.dumps({'request':self.original,'tests':contract['tests'],'reference_files':files,'failure':result.get('output','')},ensure_ascii=False)}],
                    config={'tags':['contract-reconciliation'],'callbacks':[]})
            revision=parse_generated_json(str(corrected.content))
            revised=parse_contract(json.dumps({**contract,'tests':revision['tests']}))
            count=len([n for n in ast.parse(revised['tests']).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')])
            if count<original_count:
                raise QualityBlocked('Contract correction removed coverage')
            candidate=revision.get('files',files)
            if not isinstance(candidate,dict) or not candidate or len(candidate)>10:
                raise QualityBlocked('Invalid corrected reference')
            for name,body in candidate.items():
                if not re.fullmatch(r"app/(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py",name) or not isinstance(body,str) or len(body)>24000:
                    raise QualityBlocked('Invalid corrected reference file')
            files,contract=candidate,revised
            self.events.append({'type':'contract_correction','reason':revision.get('reason',''),
                                'usage':getattr(corrected,'usage_metadata',None)})
        raise QualityBlocked('Unreachable preflight state')

    async def verify(self) -> dict:
        """Compile/import changed modules and execute frozen contract plus project tests."""
        await self.prepare()
        if not self.contract["requires_changes"]:
            return {"passed": True, "note": "No Python implementation contract"}
        current = await self.snapshot()
        changed = [p for p,h in current["files"].items() if self.initial.get(p) != h]
        source = """import pathlib,subprocess,sys,json,os,tempfile,xml.etree.ElementTree as ET
root=pathlib.Path.cwd()
changed=CHANGED
for name in changed:
 compile((root/name).read_text(encoding='utf-8'),name,'exec')
for name in changed:
 if name.startswith('app/') and name.endswith('.py'):
  module=name[:-3].replace('/','.')
  imported=subprocess.run([sys.executable,'-c','import importlib; importlib.import_module('+repr(module)+')'],capture_output=True,text=True,timeout=15)
  if imported.returncode:
   print(json.dumps({'passed':False,'stage':'import','output':imported.stderr[-8000:]}));sys.exit(1)
with tempfile.TemporaryDirectory(prefix='ibex-contract-') as directory:
 test=pathlib.Path(directory)/'test_contract.py'
 test.write_text(CONTRACT,encoding='utf-8')
 report=pathlib.Path(directory)/'results.xml'
 env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONPATH=str(root))
 result=subprocess.run([sys.executable,'-m','pytest','-q','-c',os.devnull,'--import-mode=importlib',str(test),'tests','--junitxml='+str(report)],env=env,capture_output=True,text=True,timeout=45)
 count=skipped=0
 if report.exists():
  suites=ET.parse(report).getroot()
  for suite in suites.iter('testsuite'):
   count+=int(suite.get('tests','0'));skipped+=int(suite.get('skipped','0'))
 print(json.dumps({'passed':result.returncode==0 and count>=3 and skipped==0,'stage':'tests','exit_code':result.returncode,'tests':count,'skipped':skipped,'output':(result.stdout+result.stderr)[-14000:]}))
""".replace("CHANGED", repr(changed)).replace("CONTRACT", repr(self.contract["tests"]))
        result = await self.backend.aexecute(python_command(source, self.backend.workdir), timeout=75)
        try:
            record = json.loads(result.output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            record = {"passed": False, "stage": "verification", "output": result.output[-14000:]}
        record["passed"] = bool(record.get("passed") and result.exit_code == 0)
        record["fingerprint"] = current["files"]
        self.last_verification = record
        self.events.append({"type": "verification", **record})
        return record

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """Prepare before mutation; bound repeated failures and provide fresh file context."""
        name = request.tool_call["name"]
        args = request.tool_call["args"]
        self.tool_count += 1
        if self.tool_count > 50:
            raise QualityBlocked("Tool budget exhausted; inspect diagnostic trace")
        if name in {"write_file", "edit_file", "execute", "replace_file", "task"}:
            await self.prepare()
        signature = name + json.dumps(args, sort_keys=True)
        if self.failed_calls.get(signature, 0) >= 2:
            return ToolMessage(content="Repeated identical failed call blocked. Read the current file and change the edit or use inspect_file + replace_file.",
                               tool_call_id=request.tool_call["id"], status="error")
        if name == "execute":
            command = args.get("command", "")
            # The console-script pytest entrypoint does not always add the working
            # directory to sys.path. Make module discovery explicit for every shell.
            normalized = "export PYTHONPATH=" + shlex.quote(self.backend.workdir) + '; ' + command
            request = request.override(tool_call={**request.tool_call, "args": {**args, "command": normalized}})
        result = await handler(request)
        if isinstance(result, ToolMessage):
            content = str(result.content)
            failed = result.status == "error" or content.lower().startswith("error") or bool(re.search(r"\[Command failed with exit code [1-9]\d*\]", content))
            if failed and result.status != "error":
                result = result.model_copy(update={"status": "error"})
            self.events.append({"type": "tool", "name": name, "args": args, "failed": failed, "output": content[-6000:]})
            if failed:
                self.failed_calls[signature] = self.failed_calls.get(signature, 0) + 1
                if name == "edit_file":
                    result = result.model_copy(update={"content": content + "\nRecovery: this edit did NOT apply. Call inspect_file, then replace_file using its sha256; do not repeat the same edit."})
            elif name in {"write_file", "edit_file", "replace_file"}:
                # A previously failing test may legitimately be retried after a fix.
                self.failed_calls.clear()
        return result

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: dict, runtime: Any) -> dict | None:
        """A final answer after a code change must have fresh successful test evidence."""
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        # A model that never invokes a tool must not bypass the code-task gate by
        # immediately claiming completion. Read-only answers get no test contract.
        await self.prepare()
        if not self.contract["requires_changes"]:
            return None
        current = await self.snapshot()
        checked = self.last_verification
        if not checked or checked.get("fingerprint") != current["files"]:
            checked = await self.verify()
        if checked["passed"]:
            return None
        self.final_retries += 1
        if self.final_retries > 3:
            raise QualityBlocked("Internal verification failed after three corrective rounds")
        return {"messages": [HumanMessage(content="INTERNAL VERIFICATION FAILED. This is tool feedback, not a changed user requirement. Fix the reported failure, then call verify_changes.\n" + str(checked.get("output", checked)))], "jump_to": "model"}

    def _tools(self) -> list:
        @tool
        async def verify_changes() -> str:
            """Run independently drafted requirement tests plus project tests. Call after final edits."""
            return json.dumps(await self.verify(), ensure_ascii=False)

        @tool
        async def inspect_python_api(module: str, symbol: str) -> str:
            """Inspect installed Python dependency version, signature and docstring in the sandbox."""
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) or not re.fullmatch(r"[A-Za-z_]\w*", symbol):
                raise ValueError("Use a module name and simple symbol")
            source = f"import importlib,inspect,json; m=importlib.import_module({module!r}); o=getattr(m,{symbol!r}); print(json.dumps({{'module':{module!r},'version':getattr(m,'__version__',None),'signature':str(inspect.signature(o)),'doc':(inspect.getdoc(o) or '')[:5000]}}))"
            r = await self.backend.aexecute(python_command(source, self.backend.workdir), timeout=15)
            return r.output

        @tool
        async def inspect_file(file_path: str) -> str:
            """Read full UTF-8 source and content sha256 for a safe whole-file replacement."""
            path = self._path(file_path)
            files = await self.backend.adownload_files([path])
            if not files or files[0].error or files[0].content is None:
                return "Error: file unavailable"
            content = files[0].content
            if len(content) > 80000:
                return "Error: file exceeds replacement limit"
            return json.dumps({"file_path": path, "sha256": hashlib.sha256(content).hexdigest(), "content": content.decode("utf-8")}, ensure_ascii=False)

        @tool
        async def replace_file(file_path: str, expected_sha256: str, content: str) -> str:
            """Replace an existing project source/test only if it still matches inspect_file's hash."""
            path = self._path(file_path)
            if len(content.encode()) > 80000 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                return "Error: invalid size or sha256"
            # Check and write inside one sandbox command; request workspaces have one writer.
            source = f"import pathlib,hashlib; p=pathlib.Path({path!r}); assert not p.is_symlink(), 'symlink denied'; assert hashlib.sha256(p.read_bytes()).hexdigest()=={expected_sha256!r}, 'stale content; inspect_file again'; p.write_text({content!r},encoding='utf-8'); print('replaced')"
            r = await self.backend.aexecute(python_command(source, self.backend.workdir), timeout=15)
            return r.output if r.exit_code == 0 else "Error: " + r.output
        return [verify_changes, inspect_python_api, inspect_file, replace_file]

    def _path(self, value: str) -> str:
        from pathlib import PurePosixPath
        root = PurePosixPath(self.backend.workdir)
        p = PurePosixPath(value)
        if not p.is_absolute():
            p = root / p
        if ".." in p.parts or not p.is_relative_to(root) or not p.relative_to(root).parts or p.relative_to(root).parts[0] not in {"app", "tests"}:
            raise ValueError("Only app/ and tests/ paths within the workspace are allowed")
        return str(p)
