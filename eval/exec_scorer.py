"""eval/exec_scorer.py —— 客观评分：沙箱 pytest 通过率 + 规范静态检查

复用沙箱池（create_one_sandbox / seed_project / destroy_sandbox）。
客观评分结果是 0/1（测试过则 1）——能运行验证的任务结果是确定的，不需要模糊打分。
"""
import ast

from sandbox.lifecycle import release_backend as destroy_sandbox


async def score_by_execution(agent_factory, case: dict) -> dict:
    """Evaluate the same sandbox that the newly constructed agent actually edits."""
    from uuid import uuid4
    import shlex
    agent, sandbox = await agent_factory()
    if sandbox is None:
        raise ValueError("Execution scoring requires an isolated sandbox")
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": case["issue"]}]},
            config={"configurable": {"thread_id": f"eval-{case['id']}-{uuid4().hex}"}, "recursion_limit": 60},
        )
        hidden = f"{sandbox.workdir}/.ibex_acceptance/test_acceptance.py"
        uploaded = await sandbox.aupload_files([(hidden, case["test_code"].encode("utf-8"))])
        if not uploaded or uploaded[0].error:
            raise RuntimeError("Hidden test upload failed")
        checked = await sandbox.aexecute(
            f"cd {shlex.quote(sandbox.workdir)} && python -m pytest -q tests {shlex.quote(hidden)}", timeout=60,
        )
        target = case.get("target_file", "")
        code = b""
        if target:
            downloaded = await sandbox.adownload_files([f"{sandbox.workdir}/{target}"])
            code = downloaded[0].content or b"" if downloaded else b""
        return {
            "id": case["id"], "category": "exec",
            "score": float(checked.exit_code == 0 and bool(code)),
            "static": _static_checks(code), "exit_code": checked.exit_code,
            "detail": checked.output, "artifact": code.decode("utf-8", errors="replace"),
            "answer": result["messages"][-1].content,
        }
    finally:
        await destroy_sandbox(sandbox)


def _static_checks(code: bytes) -> dict:
    """AST 静态检查：量化 AGENTS.md 铁律有没有被遵守。

    简化版：出现 float( 调用就算违规（真实项目要更精细判断是否用于金额，
    比如只在金额相关变量/函数里查；这里粗判，作为示意）。
    """
    if not code.strip():
        return {"no_float_money": False, "has_type_hints": False, "missing_code": True}
    out = {"no_float_money": True, "has_type_hints": True}
    try:
        tree = ast.parse(code.decode("utf-8", errors="replace"))
    except SyntaxError:
        return {"no_float_money": False, "has_type_hints": False, "parse_error": True}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
            out["no_float_money"] = False
        if isinstance(node, ast.FunctionDef):
            if node.returns is None or any(a.annotation is None for a in node.args.args):
                out["has_type_hints"] = False
    return out