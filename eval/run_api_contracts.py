"""Bounded four-trial comparison of generated versus maintained API contracts."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from eval.api_contract_cases import CASES, CONTRACTS, SUITE_VERSION
from eval.run_objective import run_trial, summarize


def experiment_plan() -> list[tuple[dict, str]]:
    """Reverse arm order on the second task; never retry or select a best attempt."""
    return [(CASES[0],"generated"),(CASES[0],"maintained"),
            (CASES[1],"maintained"),(CASES[1],"generated")]


def experiment_hash() -> str:
    """Bind requests, external checks, fixed contracts and execution order."""
    return hashlib.sha256(json.dumps({"cases":CASES,
        "contracts":{k:v.sha256 for k,v in CONTRACTS.items()},
        "plan":[(c['id'],arm) for c,arm in experiment_plan()]},
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()


async def run(output: Path) -> int:
    """Run one fresh sandbox per arm and stop on account failure without dropping it."""
    from agent.main import build_agent_for_service
    from infra.settings import get_settings
    if output.exists():
        raise FileExistsError("Use a fresh result path; do not replace prior evidence")
    settings=get_settings()
    report={"suite":SUITE_VERSION,"suite_sha256":experiment_hash(),
        "started_at":datetime.now(timezone.utc).isoformat(),"complete":False,
        "planned_trials":4,"samples":[],"config":{"model":settings.model_name,
        "model_tiers":settings.model_tiers,"request_timeout":settings.timeout,
        "max_retries":settings.max_retries,"task_timeout":300,"recursion_limit":80,
        "temperature":0,"team":False,"sandbox":settings.sandbox_provider},
        "source_sha256":{name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in
            ['agent/contracts.py','agent/quality.py','agent/main.py','eval/api_contract_cases.py',
             'eval/run_api_contracts.py','eval/run_objective.py']},
        "limitations":["Two tasks, one attempt per arm; no statistical significance claim",
            "Contracts and external cases assistant-authored; no human sign-off",
            "Prompt informs maintained arm of fixed tests; effect is workflow, not isolated test-source causality",
            "Main-agent tokens exclude some generated-contract calls; not a complete bill"]}
    output.parent.mkdir(parents=True,exist_ok=True)

    def save() -> None:
        report['summary']={arm:summarize([s for s in report['samples'] if s['arm']==arm])
                           for arm in ['generated','maintained']}
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

    save()
    for case,arm in experiment_plan():
        async def factory(team: bool = False) -> tuple:
            return await build_agent_for_service(team=team,
                maintained_contract=CONTRACTS[case['id']] if arm=='maintained' else None)
        sample=await run_trial(case,factory,timeout=300)
        sample.update(arm=arm,repeat=1,contract_sha256=CONTRACTS[case['id']].sha256 if arm=='maintained' else None)
        report['samples'].append(sample);save()
        print(json.dumps({k:sample[k] for k in ['id','arm','passed','elapsed_seconds']}),flush=True)
        if sample.get('account_blocked'):
            report.update(stop_reason='provider_account_unavailable',finished_at=datetime.now(timezone.utc).isoformat())
            save();return 2
    report.update(complete=True,finished_at=datetime.now(timezone.utc).isoformat());save()
    return int(not all(s['passed'] for s in report['samples']))


def main() -> None:
    """Require explicit opt-in by invoking this paid comparison with a fresh path."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    raise SystemExit(asyncio.run(run(args.output)))


if __name__=='__main__':
    main()
