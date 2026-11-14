"""Exercise completion gates, actual Python checks and bounded recovery without model APIs."""
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from agent.quality import CodingQuality, QualityBlocked, parse_contract, parse_generated_json

CONTRACT = {"requires_changes": True, "requirements": ["Return the exact supplied integer"], "tests": """
from app.example import identity
def test_zero(): assert identity(0)==0
def test_positive(): assert identity(3)==3
def test_negative(): assert identity(-2)==-2
"""}


class LocalProbe:
    """Test-only adapter executes controller-generated code in an owned temp directory."""
    def __init__(self, path: Path) -> None:
        self.workdir = path.as_posix()

    async def aexecute(self, command: str, timeout: int = 15) -> NS:
        """Extract the quoted Python argument without invoking a shell."""
        args = shlex.split(command)
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", args[-1]],
            cwd=self.workdir, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
        return NS(exit_code=result.returncode, output=result.stdout + result.stderr)

    async def adownload_files(self, paths: list[str]) -> list:
        """Return current file bytes for replacement tools."""
        return [NS(content=Path(p).read_bytes(), error=None) for p in paths]


def project(tmp_path: Path) -> LocalProbe:
    """Create an isolated project whose implementation initially violates the contract."""
    (tmp_path/"app").mkdir()
    (tmp_path/"app"/"__init__.py").write_text("")
    (tmp_path/"app"/"example.py").write_text("def identity(value: int) -> int:\n    return 0\n")
    (tmp_path/"tests").mkdir()
    (tmp_path/"tests"/"test_basic.py").write_text("def test_seed(): assert True\n")
    return LocalProbe(tmp_path)


def test_contract_rejects_vacuous_and_disabled_tests() -> None:
    """Missing assertions or skip directives cannot silently bypass preparation."""
    for data in [{}, {"requires_changes":True,"requirements":[],"tests":""},
                 {**CONTRACT,"tests":CONTRACT['tests']+'\npytest.skip(\"skip\")'}]:
        with pytest.raises(ValueError): parse_contract(json.dumps(data))
    assert parse_contract(json.dumps(CONTRACT))['requires_changes']


def test_generated_source_json_allows_literal_newlines_but_not_bad_syntax() -> None:
    """Provider formatting artifacts are parsed as data without evaluating returned text."""
    raw='{"files":{"app/a.py":"def f():\n    return 1\n"}}'
    assert parse_generated_json(raw)['files']['app/a.py']=='def f():\n    return 1\n'
    for invalid in ['[]',"__import__('os').system('whoami')",'{files:bad}']:
        with pytest.raises(ValueError): parse_generated_json(invalid)


def test_contract_is_frozen_and_real_verification_invalidated(tmp_path: Path) -> None:
    """Broken code fails, a real fix passes, and a later mutation invalidates evidence."""
    backend=project(tmp_path)
    model=NS(ainvoke=AsyncMock(side_effect=[AIMessage(content=json.dumps(CONTRACT)),
        AIMessage(content=json.dumps({'additional_tests':'','coverage_notes':[]})),
        AIMessage(content=json.dumps({'files':{'app/example.py':'def identity(value): return value\n'}}))]))
    quality=CodingQuality(backend,lambda:model)
    async def run() -> None:
        await quality.abefore_agent({'messages':[HumanMessage(content='Implement identity')]},None)
        first=await quality.verify()
        assert first['passed'] is False and first['tests']==4
        assert len(quality.events)==5
        (tmp_path/'app'/'example.py').write_text('def identity(value: int) -> int:\n    """Return unchanged."""\n    return value\n')
        assert (await quality.verify())['passed'] is True
        final={'messages':[AIMessage(content='done')]}
        assert await quality.aafter_model(final,None) is None
        (tmp_path/'app'/'example.py').write_text('def identity(value: int) -> int:\n    return None\n')
        corrected=await quality.aafter_model(final,None)
        assert corrected['jump_to']=='model'
        assert quality.last_verification['passed'] is False
        assert model.ainvoke.await_count==3
    asyncio.run(run())


def test_import_errors_and_skips_fail_verification(tmp_path: Path) -> None:
    """Import-time API incompatibilities and skipped tests are visible failures."""
    backend=project(tmp_path)
    quality=CodingQuality(backend,lambda:None)
    quality.contract=CONTRACT
    async def run() -> None:
        (tmp_path/'app'/'example.py').write_text('raise TypeError("unsupported argument")\n')
        result=await quality.verify()
        assert not result['passed'] and result['stage']=='import'
        (tmp_path/'app'/'example.py').write_text('def identity(value): return value\n')
        (tmp_path/'tests'/'test_basic.py').write_text('import pytest\n@pytest.mark.skip\ndef test_seed(): pass\n')
        result=await quality.verify()
        assert not result['passed'] and result['skipped']==1
    asyncio.run(run())


def test_dependency_inspection_uses_installed_signature(tmp_path: Path) -> None:
    """The API helper reports the runtime signature rather than a model's memory."""
    quality=CodingQuality(project(tmp_path),lambda:None)
    inspect={t.name:t for t in quality.tools}['inspect_python_api']
    result=json.loads(asyncio.run(inspect.ainvoke({'module':'pydantic','symbol':'confloat'})))
    assert 'allow_inf_nan' in result['signature']
    assert 'finite=' not in result['signature']


def test_contract_review_failure_discloses_reduced_scope(tmp_path: Path) -> None:
    """Malformed generated tests do not become an authoritative requirements oracle."""
    model=NS(ainvoke=AsyncMock(side_effect=[AIMessage(content=json.dumps(CONTRACT)),AIMessage(content='not-json')]))
    quality=CodingQuality(project(tmp_path),lambda:model)
    asyncio.run(quality.prepare())
    assert quality.contract['scope']=='project_only'
    assert quality.contract['tests']==''
    assert quality.events[-1]['type']=='contract_unavailable'


def test_project_only_fallback_still_checks_tests_and_discloses_scope(tmp_path: Path) -> None:
    """An unreliable generated contract cannot silently turn into full verification."""
    backend=project(tmp_path)
    quality=CodingQuality(backend,lambda:None)
    quality.contract={'requires_changes':True,'tests':'','scope':'project_only'}
    async def run() -> None:
        result=await quality.verify()
        assert result['passed'] and result['scope']=='project_only'
        final=await quality.aafter_model({'messages':[AIMessage(content='done',id='answer')]},None)
        assert final['messages'][0].id=='answer'
        assert '未据此验证完整需求' in final['messages'][0].content
        (tmp_path/'tests'/'test_basic.py').write_text('def test_seed(): assert False\n')
        result=await quality.verify()
        assert not result['passed']
    asyncio.run(run())


def test_bad_oracle_is_blocked_before_candidate_mutation(tmp_path: Path) -> None:
    """An unresolved test/reference disagreement cannot coerce the candidate into a bad fix."""
    bad={**CONTRACT,'tests':CONTRACT['tests'].replace('identity(3)==3','identity(3)==7')}
    reference={'app/example.py':'def identity(value): return value\n'}
    model=NS(ainvoke=AsyncMock(side_effect=[AIMessage(content=json.dumps({'files':reference})),
        AIMessage(content=json.dumps({'files':reference,'tests':bad['tests'],'reason':'unresolved'}))]))
    backend=project(tmp_path)
    original=(tmp_path/'app'/'example.py').read_text()
    quality=CodingQuality(backend,lambda:model)
    with pytest.raises(QualityBlocked,match='disagreement'):
        asyncio.run(quality.preflight_contract(bad,{}))
    assert (tmp_path/'app'/'example.py').read_text()==original
    assert [e['passed'] for e in quality.events if e['type']=='contract_preflight']==[False,False]


def test_replacement_checks_hash_and_scope(tmp_path: Path) -> None:
    """Whole-file recovery rejects stale content and directory traversal."""
    backend=project(tmp_path)
    quality=CodingQuality(backend,lambda:None)
    tool={t.name:t for t in quality.tools}['replace_file']
    file=tmp_path/'app'/'example.py'
    initial=file.read_bytes()
    async def run() -> None:
        request={'file_path':'app/example.py','expected_sha256':'0'*64,'content':'changed'}
        assert (await tool.ainvoke(request)).startswith('Error:')
        assert file.read_bytes()==initial
        request['expected_sha256']=hashlib.sha256(initial).hexdigest()
        assert (await tool.ainvoke(request)).strip()=='replaced'
        assert file.read_text()=='changed'
        request['file_path']='../outside.py'
        with pytest.raises(ValueError): await tool.ainvoke(request)
    asyncio.run(run())


def test_repeated_failed_edits_are_bounded() -> None:
    """Two identical failures are passed through; the third is blocked before execution."""
    quality=CodingQuality(NS(workdir='/home/agent'),lambda:None)
    quality.prepare=AsyncMock()
    request=NS(tool_call={'name':'edit_file','args':{'file_path':'/home/agent/app/a.py'},'id':'edit'})
    handler=AsyncMock(return_value=ToolMessage(content='Error: text not found',tool_call_id='edit'))
    async def run() -> None:
        for _ in range(2):
            assert 'Recovery' in str((await quality.awrap_tool_call(request,handler)).content)
        assert 'blocked' in str((await quality.awrap_tool_call(request,handler)).content)
        assert handler.await_count==2
    asyncio.run(run())


def test_shell_exit_failure_is_detected_and_fix_allows_retry() -> None:
    """Real ToolCallRequest overrides preserve imports and distinguish retry after edits."""
    from langchain.tools.tool_node import ToolCallRequest
    quality=CodingQuality(NS(workdir='/home/agent'),lambda:None)
    quality.prepare=AsyncMock()
    request=ToolCallRequest(tool_call={'name':'execute','args':{'command':'pytest tests'},'id':'x'},tool=None,state={},runtime=None)
    handler=AsyncMock(return_value=ToolMessage(content='test failed\n[Command failed with exit code 1]',tool_call_id='x'))
    async def run() -> None:
        for _ in range(2):
            assert (await quality.awrap_tool_call(request,handler)).status=='error'
        actual=handler.call_args.args[0].tool_call['args']['command']
        assert actual=='export PYTHONPATH=/home/agent; pytest tests'
        assert 'blocked' in (await quality.awrap_tool_call(request,handler)).content
        edited=request.override(tool_call={'name':'edit_file','args':{},'id':'edit'})
        await quality.awrap_tool_call(edited,AsyncMock(return_value=ToolMessage(content='replaced',tool_call_id='edit')))
        await quality.awrap_tool_call(request,handler)
        assert handler.await_count==3
    asyncio.run(run())


def test_final_failure_is_not_success() -> None:
    """A stubborn model cannot claim completion indefinitely after failed tests."""
    quality=CodingQuality(NS(workdir='/home/agent'),lambda:None)
    quality.contract=CONTRACT
    quality.snapshot=AsyncMock(return_value={'files':{}})
    quality.last_verification={'passed':False,'fingerprint':{},'output':'assertion failed'}
    async def run() -> None:
        for _ in range(3):
            assert (await quality.aafter_model({'messages':[AIMessage(content='done')]},None))['jump_to']=='model'
        with pytest.raises(QualityBlocked):
            await quality.aafter_model({'messages':[AIMessage(content='done')]},None)
    asyncio.run(run())


def test_real_graph_routes_failed_completion_back_to_model() -> None:
    """Exercise LangGraph routing, not just direct middleware method calls."""
    from langchain.agents import create_agent
    from tests_service.test_agent_mechanisms import ScriptedModel
    quality=CodingQuality(NS(workdir='/home/agent'),lambda:None)
    quality.contract=CONTRACT
    quality.snapshot=AsyncMock(return_value={'files':{}})
    async def verify() -> dict:
        quality.last_verification={'passed':quality.final_retries>0,'fingerprint':{},'output':'fix required'}
        # force re-verification on second final response for this scripted test
        if not quality.last_verification['passed']:
            quality.last_verification['fingerprint']={'old':'version'}
        return quality.last_verification
    quality.verify=verify
    graph=create_agent(model=ScriptedModel(responses=[AIMessage(content='done prematurely'),AIMessage(content='fixed')]),middleware=[quality])
    result=asyncio.run(graph.ainvoke({'messages':[HumanMessage(content='implement')]}))
    assert result['messages'][-1].content=='fixed'
    assert quality.final_retries==1
    assert any('INTERNAL VERIFICATION FAILED' in str(m.content) for m in result['messages'])


def test_immediate_completion_cannot_skip_contract() -> None:
    """No-tool answers still classify the task before accepting a completion."""
    quality=CodingQuality(NS(workdir='/home/agent'),lambda:None)
    async def prepare() -> None:
        quality.contract=CONTRACT
    quality.prepare=prepare
    quality.snapshot=AsyncMock(return_value={'files':{}})
    quality.verify=AsyncMock(return_value={'passed':False,'output':'missing implementation'})
    result=asyncio.run(quality.aafter_model({'messages':[AIMessage(content='already done')]},None))
    assert result['jump_to']=='model'
