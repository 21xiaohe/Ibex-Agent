"""Check deterministic contract behavior and both API oracles without paid calls."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage
from agent.contracts import MaintainedContract
from agent.quality import CodingQuality, QualityBlocked
from eval.api_contract_cases import CASES, CONTRACTS
from eval.run_objective import acceptance_command
from tests_service.test_coding_quality import LocalProbe, project, CONTRACT


REFERENCES = {
"shipping-api": '''from decimal import Decimal, InvalidOperation, ROUND_CEILING
from fastapi import FastAPI, HTTPException
app=FastAPI()
@app.get('/health')
def health() -> dict:
    """Return service status."""
    return {'status':'ok'}
@app.post('/shipping-quote')
def quote(body: dict) -> dict:
    """Calculate a deterministic Decimal shipping fee."""
    if set(body)-{'weightKg','zone','insured'}: raise HTTPException(422)
    value=body.get('weightKg'); zone=body.get('zone'); insured=body.get('insured',False)
    if type(value) is not str or type(insured) is not bool or zone not in ('local','remote'): raise HTTPException(422)
    try: weight=Decimal(value)
    except (InvalidOperation,ValueError): raise HTTPException(422)
    if not weight.is_finite() or not Decimal('0')<weight<=Decimal('30'): raise HTTPException(422)
    extra=max(Decimal('0'),weight-Decimal('1')).to_integral_value(rounding=ROUND_CEILING)
    fee=(Decimal('5') if zone=='local' else Decimal('12'))+extra*Decimal('2')+(Decimal('3') if insured else Decimal('0'))
    return {'fee':format(fee,'.2f')}
''',
"booking-api": '''from datetime import date
from fastapi import FastAPI, HTTPException
app=FastAPI()
@app.get('/health')
def health() -> dict:
    """Return service status."""
    return {'status':'ok'}
@app.post('/booking-check')
def booking(body: dict) -> dict:
    """Validate an appointment without storing state."""
    if set(body)!={'date','startMinute','endMinute','seats'}: raise HTTPException(422)
    day=body['date']; start=body['startMinute']; end=body['endMinute']; seats=body['seats']
    if type(day) is not str or any(type(x) is not int for x in (start,end,seats)): raise HTTPException(422)
    try:
        parsed=date.fromisoformat(day)
        if parsed.isoformat()!=day: raise ValueError('canonical')
    except ValueError: raise HTTPException(422)
    if not (0<=start<end<=1440 and start%15==0 and end%15==0 and 15<=end-start<=180 and 1<=seats<=8): raise HTTPException(422)
    return {'date':day,'durationMinutes':end-start,'seatMinutes':(end-start)*seats}
''',
}
MUTATIONS = {
"shipping-api": [
    ("rounding=ROUND_CEILING", "rounding='ROUND_FLOOR'"),
    ("type(value) is not str", "False"),
    ("if set(body)-{'weightKg','zone','insured'}:", "if False:"),
],
"booking-api": [
    ("type(x) is not int", "not isinstance(x,int)"),
    ("parsed.isoformat()!=day", "False"),
    ("end-start<=180", "end-start<180"),
],
}


@pytest.mark.parametrize('case', CASES, ids=[c['id'] for c in CASES])
def test_api_contracts_and_external_checks_reject_semantic_mutations(case: dict, tmp_path: Path) -> None:
    """Both test sets accept the reference and reject distinct realistic defects."""
    (tmp_path/'app').mkdir(); (tmp_path/'app'/'__init__.py').write_text('')
    target=tmp_path/case['target_file']
    reference=REFERENCES[case['id']]
    for tests in [CONTRACTS[case['id']].tests, case['tests']]:
        cmd=[sys.executable,'-B',*shlex.split(acceptance_command({**case,'tests':tests},str(tmp_path)))[1:]]
        target.write_text(reference,encoding='utf-8')
        env={**__import__('os').environ,'PYTHONDONTWRITEBYTECODE':'1'}
        result=subprocess.run(cmd,capture_output=True,text=True,timeout=15,env=env)
        assert result.returncode==0,result.stdout+result.stderr
        for old,new in MUTATIONS[case['id']]:
            assert old in reference
            target.write_text(reference.replace(old,new),encoding='utf-8')
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=15,env=env)
            assert result.returncode!=0,(case['id'],old,'mutation survived')


def test_maintained_gate_returns_failure_and_checks_repair_without_model_calls(tmp_path: Path) -> None:
    """A real bad implementation is routed to repair; fixed code passes frozen tests."""
    backend=project(tmp_path)
    contract=MaintainedContract('identity','Implement identity',tuple(CONTRACT['requirements']),CONTRACT['tests'],'test fixture')
    model=AsyncMock(side_effect=AssertionError('must not generate a contract'))
    quality=CodingQuality(backend,model,contract)
    async def run() -> None:
        await quality.abefore_agent({'messages':[HumanMessage(content=contract.request)]},None)
        result=await quality.aafter_model({'messages':[AIMessage(content='done')]},None)
        assert result['jump_to']=='model'
        assert not quality.last_verification['passed']
        assert quality.last_verification['scope']=='maintained_contract_and_project'
        (tmp_path/'app'/'example.py').write_text('def identity(value): return value\n')
        assert await quality.aafter_model({'messages':[AIMessage(content='done')]},None) is None
        assert quality.last_verification['passed']
        assert quality.events[0]['sha256']==contract.sha256
        model.assert_not_called()
    asyncio.run(run())


def test_contract_mismatch_and_invalid_tests_never_fall_back(tmp_path: Path) -> None:
    """Reject using the right tests for the wrong request or silently weakening a fixed contract."""
    contract=MaintainedContract('identity','right',tuple(CONTRACT['requirements']),CONTRACT['tests'],'test fixture')
    quality=CodingQuality(project(tmp_path),lambda:None,contract)
    quality.original='wrong'
    with pytest.raises(QualityBlocked): asyncio.run(quality.prepare())
    quality.original='right';quality.maintained_contract=replace(contract,tests='')
    with pytest.raises(ValueError): asyncio.run(quality.prepare())
    assert quality.contract is None
    assert not any(e['type']=='contract_unavailable' for e in quality.events)


def test_external_examples_are_distinct_and_never_in_contract() -> None:
    """Candidate diagnostics can reveal fixed tests, but not the external examples."""
    for case in CASES:
        assert case['tests']!=CONTRACTS[case['id']].tests
        assert case['tests'] not in case['issue']
    assert '29.5' not in CONTRACTS['shipping-api'].tests
    assert '2030-12-31' not in CONTRACTS['booking-api'].tests


def test_comparison_preserves_failures_and_stops_on_account_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The paid runner keeps the failed arm and never continues after an account failure."""
    from eval import run_api_contracts as runner
    mock=AsyncMock(side_effect=[
        {'id':CASES[0]['id'],'passed':False,'elapsed_seconds':1},
        {'id':CASES[0]['id'],'passed':False,'elapsed_seconds':2,'account_blocked':True},
    ])
    monkeypatch.setattr(runner,'run_trial',mock)
    output=tmp_path/'partial.json'
    assert asyncio.run(runner.run(output))==2
    report=json.loads(output.read_text(encoding='utf-8'))
    assert not report['complete'] and report['planned_trials']==4
    assert len(report['samples'])==2 and mock.await_count==2
    assert report['summary']['generated']['trials']==1
    assert report['summary']['maintained']['trials']==1
    with pytest.raises(FileExistsError): asyncio.run(runner.run(output))


def test_service_builder_passes_controller_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real graph builder receives the optional contract; API schemas stay unchanged."""
    from agent import main
    from types import SimpleNamespace
    from unittest.mock import Mock
    contract=CONTRACTS['shipping-api']
    monkeypatch.setattr(main.get_settings(),'sandbox_provider','docker')
    backend=SimpleNamespace(workdir='/workspace')
    monkeypatch.setattr(main,'create_backend',AsyncMock(return_value=backend))
    monkeypatch.setattr(main.MCPManager,'from_settings',lambda:SimpleNamespace(get_tools=AsyncMock(return_value=[])))
    build=Mock(return_value=object());monkeypatch.setattr(main,'_build',build)
    asyncio.run(main.build_agent_for_service(team=False,maintained_contract=contract))
    assert build.call_args.kwargs['maintained_contract'] is contract
