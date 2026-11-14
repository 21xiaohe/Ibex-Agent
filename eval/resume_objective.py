"""Continue only unexecuted trials after account recovery, preserving all failures."""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable

from eval import run_objective as runner


def continuation_plan(report: dict, config: dict) -> list[tuple[int, dict]]:
    """Reject changed scoring/configuration or non-prefix samples before paid calls."""
    if report.get("complete") is not False or report.get("stop_reason") != "provider_account_unavailable":
        raise ValueError("Only an account-interrupted report can be continued")
    if report.get("suite_sha256") != runner.suite_hash() or report.get("config") != config:
        raise ValueError("Task set or model configuration changed")
    if report.get("runner_sha256") != hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest():
        raise ValueError("Scoring runner changed")
    plan = [(repeat + 1, case) for repeat in range(config["repeats"]) for case in runner.CASES]
    samples = report["samples"]
    if report.get("planned_trials") != len(plan) or not 0 < len(samples) < len(plan):
        raise ValueError("Invalid planned/completed trial count")
    for sample, (repeat, case) in zip(samples, plan):
        if (sample["repeat"], sample["id"]) != (repeat, case["id"]):
            raise ValueError("Samples must be an unchanged execution prefix")
    if not samples[-1].get("account_blocked"):
        raise ValueError("Missing terminal account failure")
    return plan[len(samples):]


async def continue_report(source: Path, output: Path, config: dict, factory: Callable) -> int:
    """Write a new evidence file and retain the interrupted trial in the denominator."""
    if output.exists():
        raise FileExistsError("Never overwrite prior evidence")
    raw = source.read_bytes()
    original = json.loads(raw)
    plan = continuation_plan(original, config)
    report = copy.deepcopy(original)
    report.setdefault("continuations", []).append({
        "source_name": source.name, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "at": datetime.now(timezone.utc).isoformat(), "retained_trials": len(report["samples"]),
        "reason": report.pop("stop_reason"),
        "continuation_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    })
    report.pop("finished_at", None)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    for repeat, case in plan:
        sample = await runner.run_trial(case, factory, config["timeout_seconds"])
        sample["repeat"] = repeat
        report["samples"].append(sample)
        report["summary"] = runner.summarize(report["samples"])
        save()
        print(json.dumps({k: sample[k] for k in ("id", "repeat", "passed", "elapsed_seconds")}), flush=True)
        if sample.get("account_blocked"):
            report.update(stop_reason="provider_account_unavailable", finished_at=datetime.now(timezone.utc).isoformat())
            save()
            return 2
    report.update(complete=True, finished_at=datetime.now(timezone.utc).isoformat())
    save()
    return int(report["summary"]["task_success_rate"] < 1)


def main() -> None:
    """Require an explicit source and fresh destination after the user restores access."""
    from agent.main import build_agent_for_service
    from infra.settings import get_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    old = json.loads(args.source.read_text(encoding="utf-8"))
    settings = get_settings()
    config = dict(old["config"], model=settings.model_name, provider=settings.model_provider,
                  sandbox=settings.sandbox_provider, team=False, temperature=0)
    environment = {"model_tiers": settings.model_tiers, "request_timeout": settings.timeout,
                   "max_retries": settings.max_retries}
    if old.get("environment") != environment:
        raise ValueError("Model tiers or request limits changed")
    raise SystemExit(asyncio.run(continue_report(args.source, args.output, config, build_agent_for_service)))


if __name__ == "__main__":
    main()
