"""Quality-gate regressions, without paid model calls."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from eval.objective_cases import CASES
from eval.run_objective import acceptance_command, run_trial, summarize, suite_hash


def test_suite_has_unique_ids_and_compilable_tests() -> None:
    """Frozen tasks cover distinct behaviors and include executable acceptance tests."""
    assert len(CASES) == len({c['id'] for c in CASES}) == 6
    for case in CASES:
        compile(case['tests'], case['id'], 'exec')
        assert 'test_' in case['tests']
        assert case['tests'] not in case['issue']
        assert acceptance_command(case, '/home/agent').startswith('python -I -c ')
    assert len(suite_hash()) == 64


def test_failures_stay_in_denominator() -> None:
    """A failed attempt cannot disappear or become best-of-two success."""
    samples=[{'id':'a','passed':True,'elapsed_seconds':1},
             {'id':'a','passed':False,'elapsed_seconds':3,'error_type':'TimeoutError'}]
    result=summarize(samples)
    assert result['task_success_rate'] == .5
    assert result['by_case']['a'] == {'passed':1,'trials':2}


@pytest.mark.parametrize('exit_code,assertions,artifact,expected', [
    (0,[{'name':'test_ok','passed':True}],b'code',True),
    (1,[{'name':'test_ok','passed':True}],b'code',False),
    (0,[{'name':'test_bad','passed':False}],b'code',False),
    (0,[],b'code',False),
    (0,[{'name':'test_ok','passed':True}],b'',False),
])
def test_score_requires_exit_status_assertions_and_artifact(monkeypatch: pytest.MonkeyPatch,
    exit_code: int, assertions: list, artifact: bytes, expected: bool) -> None:
    """Completion prose or a misleading marker cannot alone make a trial pass."""
    from sandbox import lifecycle
    cleanup=AsyncMock()
    monkeypatch.setattr(lifecycle,'release_backend',cleanup)
    backend=NS(workdir='/home/agent',
        adownload_files=AsyncMock(return_value=[NS(error=None,content=artifact)]),
        aexecute=AsyncMock(return_value=NS(exit_code=exit_code,output='IBEX_ACCEPTANCE='+json.dumps(assertions))))
    agent=NS(ainvoke=AsyncMock(return_value={'messages':[]}))
    result=asyncio.run(run_trial(CASES[0],AsyncMock(return_value=(agent,backend))))
    assert result['passed'] is expected
    cleanup.assert_awaited_once_with(backend)


def test_timeout_releases_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timed out model execution is a failed trial and still destroys the sandbox."""
    from sandbox import lifecycle
    cleanup=AsyncMock()
    monkeypatch.setattr(lifecycle,'release_backend',cleanup)
    async def hang(*args, **kwargs):
        await asyncio.sleep(10)
    backend=NS(workdir='/home/agent')
    result=asyncio.run(run_trial(CASES[0],AsyncMock(return_value=(NS(ainvoke=hang),backend)),timeout=.01))
    assert not result['passed'] and result['error_type']=='TimeoutError'
    cleanup.assert_awaited_once_with(backend)
