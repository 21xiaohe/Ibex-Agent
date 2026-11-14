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


def test_failed_trial_saves_artifact_and_trace_before_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quality-gate failure retains partial code and tool evidence without becoming a pass."""
    from sandbox import lifecycle
    from agent.quality import QualityBlocked
    order=[]
    async def download(paths: list) -> list:
        order.append('download')
        return [NS(content=b'partial code',error=None)]
    async def cleanup(backend: object) -> None:
        order.append('cleanup')
    monkeypatch.setattr(lifecycle,'release_backend',cleanup)
    backend=NS(workdir='/home/agent',adownload_files=download)
    agent=NS(ainvoke=AsyncMock(side_effect=QualityBlocked('failed')),
             ibex_quality=NS(events=[{'type':'verification','passed':False}],last_verification={'passed':False}))
    result=asyncio.run(run_trial(CASES[0],AsyncMock(return_value=(agent,backend))))
    assert result['error_type']=='QualityBlocked' and not result['passed']
    assert result['artifact']=='partial code' and result['quality_trace'][0]['passed'] is False
    assert order==['download','cleanup']


def test_account_error_is_classified_without_recording_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an allowlisted provider code, never the raw HTTP response or secret-bearing text."""
    from sandbox import lifecycle
    class AccountError(Exception):
        body={'code':'Arrearage','message':'sensitive-provider-details'}
    monkeypatch.setattr(lifecycle,'release_backend',AsyncMock())
    result=asyncio.run(run_trial(CASES[0],AsyncMock(side_effect=AccountError('private'))))
    assert result['account_blocked'] and result['provider_error_code']=='Arrearage'
    assert 'sensitive-provider-details' not in json.dumps(result)


def test_account_failure_stops_remaining_paid_trials(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A persistent account block ends the run as incomplete instead of hammering the API."""
    from eval import run_objective
    trial=AsyncMock(return_value={'id':CASES[0]['id'],'passed':False,'elapsed_seconds':1,
                                 'account_blocked':True,'provider_error_code':'Arrearage'})
    monkeypatch.setattr(run_objective,'CASES',[CASES[0]])
    monkeypatch.setattr(run_objective,'run_trial',trial)
    output=tmp_path/'blocked.json'
    args=NS(output=str(output),repeats=2,timeout=300,baseline=None,min_success_rate=1)
    assert asyncio.run(run_objective.run(args))==2
    data=json.loads(output.read_text(encoding='utf-8'))
    assert data['complete'] is False and data['planned_trials']==2
    assert len(data['samples'])==1 and trial.await_count==1
