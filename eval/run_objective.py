"""Run the frozen development suite; persist all trials, including failures.

Usage: python -m eval.run_objective --repeats 2 --output logs/objective-v1.json
This calls the configured model and creates disposable sandboxes. No LLM judge.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import time
from typing import Any, Callable
from uuid import uuid4

from eval.objective_cases import CASES, SUITE_VERSION


def suite_hash() -> str:
    """Hash prompts, fixtures and withheld tests so comparisons identify the task set."""
    return hashlib.sha256(json.dumps(CASES, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def acceptance_command(case: dict, workdir: str) -> str:
    """Run trusted assertions after generation, ignoring project pytest configuration."""
    # Import the installed test dependency in isolated mode BEFORE adding the app path.
    # The model never receives tests or scoring code during generation.
    source = (
        "import sys,json,pytest\n"
        f"sys.path.insert(0,{workdir!r})\n"
        f"scope={{'pytest':pytest}}\nexec({case['tests']!r},scope)\n"
        "results=[]\n"
        "for name,fn in list(scope.items()):\n"
        " if name.startswith('test_') and callable(fn):\n"
        "  try:\n   fn()\n   results.append({'name':name,'passed':True})\n"
        "  except Exception as e:\n   results.append({'name':name,'passed':False,'error':type(e).__name__})\n"
        "print('IBEX_ACCEPTANCE='+json.dumps(results))\n"
        "sys.exit(0 if results and all(x['passed'] for x in results) else 1)\n"
    )
    return "python -I -c " + shlex.quote(source)


def summarize(samples: list[dict]) -> dict:
    """Keep infrastructure failures in the denominator; do not report best-of-N."""
    passed = sum(item.get("passed", False) for item in samples)
    return {"trials": len(samples), "passed": passed,
            "task_success_rate": passed / len(samples) if samples else 0.0,
            "median_seconds": statistics.median([s["elapsed_seconds"] for s in samples]) if samples else 0,
            "by_case": {identifier: {"passed": sum(s.get("passed", False) for s in samples if s["id"] == identifier),
                                      "trials": sum(s["id"] == identifier for s in samples)}
                        for identifier in sorted({s["id"] for s in samples})}}


async def run_trial(case: dict, factory: Callable, timeout: float = 300) -> dict:
    """Generate in one owned sandbox, then test the actual generated artifact."""
    from sandbox.lifecycle import release_backend
    from eval.exec_scorer import _static_checks
    started = time.monotonic()
    sandbox = None
    agent = None
    record: dict = {"id": case["id"], "category": case["category"], "passed": False}
    try:
        async with asyncio.timeout(timeout):
            agent, sandbox = await factory(team=False)
            if sandbox is None:
                raise RuntimeError("An isolated sandbox is required")
            if case.get("setup"):
                uploaded = await sandbox.aupload_files([(sandbox.workdir + "/" + name, body.encode()) for name, body in case["setup"].items()])
                if any(item.error for item in uploaded):
                    raise RuntimeError("Fixture upload failed")
            response = await agent.ainvoke(
                {"messages": [{"role": "user", "content": case["issue"]}]},
                config={"configurable": {"thread_id": "objective-" + uuid4().hex}, "recursion_limit": 80})
            files = await sandbox.adownload_files([sandbox.workdir + "/" + case["target_file"]])
            artifact = files[0].content if files and not files[0].error else None
            checked = await sandbox.aexecute(acceptance_command(case, sandbox.workdir), timeout=45)
            assertions = []
            for line in checked.output.splitlines():
                if line.startswith("IBEX_ACCEPTANCE="):
                    assertions = json.loads(line.split("=", 1)[1])
            record.update(passed=checked.exit_code == 0 and bool(artifact) and bool(assertions) and all(t["passed"] for t in assertions),
                          acceptance=assertions, exit_code=checked.exit_code, detail=checked.output[-12000:],
                          artifact=(artifact or b"").decode("utf-8", errors="replace"),
                          static_advisory=_static_checks(artifact or b""),
                          input_tokens=response.get("total_input_tokens"), output_tokens=response.get("total_output_tokens"),
                          tool_calls=sum(len(getattr(m, "tool_calls", []) or []) for m in response.get("messages", [])))
    except Exception as error:
        record.update(error_type=type(error).__name__)
        body = getattr(error, "body", None)
        details = body.get("error", body) if isinstance(body, dict) else {}
        code = details.get("code") if isinstance(details, dict) else None
        if code in {"Arrearage", "insufficient_quota"}:
            record["provider_error_code"] = code
            record["account_blocked"] = True
    finally:
        # Capture diagnostics BEFORE destroying the owned sandbox, even after timeout.
        # This does not change the frozen acceptance command or success criterion.
        if sandbox is not None and "artifact" not in record:
            try:
                files = await asyncio.wait_for(sandbox.adownload_files([sandbox.workdir + "/" + case["target_file"]]), timeout=10)
                record["artifact"] = (files[0].content or b"").decode("utf-8", errors="replace") if files else ""
            except Exception as error:
                record["artifact_capture_error"] = type(error).__name__
        quality = getattr(agent, "ibex_quality", None)
        if quality is not None:
            record["quality_trace"] = quality.events
            record["internal_verification_passed"] = bool(quality.last_verification and quality.last_verification.get("passed"))
            record["internal_verification_scope"] = (quality.last_verification or {}).get("scope")
        try:
            await release_backend(sandbox)
        except Exception as error:
            record.update(passed=False, cleanup_error=type(error).__name__)
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record


async def run(args: argparse.Namespace) -> int:
    """Write partial results after every trial and never silently replace a baseline."""
    from agent.main import build_agent_for_service
    from infra.settings import get_settings
    settings = get_settings()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError("Choose a fresh output path; prior evidence is immutable")
    output.parent.mkdir(parents=True, exist_ok=True)
    config = {"model": settings.model_name, "provider": settings.model_provider,
              "sandbox": settings.sandbox_provider, "team": False, "temperature": 0,
              "timeout_seconds": args.timeout, "repeats": args.repeats}
    report: dict = {"suite": SUITE_VERSION, "suite_sha256": suite_hash(), "config": config,
                   "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "started_at": datetime.now(timezone.utc).isoformat(), "samples": []}
    report["planned_trials"] = len(CASES) * args.repeats
    report["environment"] = {"model_tiers": settings.model_tiers, "request_timeout": settings.timeout,
                             "max_retries": settings.max_retries}
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8")) if args.baseline else None
    if baseline and (baseline["suite_sha256"] != report["suite_sha256"] or baseline["config"] != config or baseline["runner_sha256"] != report["runner_sha256"]):
        raise ValueError("Baseline suite, runner or configuration does not match")
    for repeat in range(args.repeats):
        for case in CASES:
            sample = await run_trial(case, build_agent_for_service, args.timeout)
            sample["repeat"] = repeat + 1
            report["samples"].append(sample)
            report["summary"] = summarize(report["samples"])
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: sample[k] for k in ("id", "repeat", "passed", "elapsed_seconds")}), flush=True)
            if sample.get("account_blocked"):
                report.update(complete=False, stop_reason="provider_account_unavailable",
                              finished_at=datetime.now(timezone.utc).isoformat())
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                return 2
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["complete"] = True
    if baseline:
        report["baseline_comparison"] = {"path": args.baseline,
            "success_rate_delta": report["summary"]["task_success_rate"] - baseline["summary"]["task_success_rate"]}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"]), flush=True)
    return int(report["summary"]["task_success_rate"] < args.min_success_rate)


def main() -> None:
    """Parse explicit paid-run limits and return a nonzero result on unmet quality gates."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--min-success-rate", type=float, default=1.0)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5 or not 10 <= args.timeout <= 600 or not 0 <= args.min_success_rate <= 1:
        parser.error("Invalid limits: repeats 1..5, timeout 10..600, success rate 0..1")
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
