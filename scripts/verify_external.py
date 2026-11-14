"""Verify authorized cloud credentials with isolated disposable resources."""
import json
import os
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values


def main() -> None:
    """Test Daytona lifecycle and LangSmith tracing, recording no credentials."""
    config = dotenv_values("D:/ShanYangClaw/shanyang-claw/.env")
    results = {}
    for name in ("DAYTONA_API_KEY", "DAYTONA_TARGET"):
        if config.get(name):
            os.environ[name] = config[name]
    sandbox = client = None
    try:
        from sandbox.manager import get_or_create_sandbox_backend
        backend, sandbox, client, workdir = get_or_create_sandbox_backend("verify-" + uuid4().hex)
        reused_backend, reused, _, _ = get_or_create_sandbox_backend(sandbox.labels["thread_id"])
        reuse_ok = reused.id == sandbox.id
        wrote = backend.upload_files([(f"{workdir}/verification.txt", b"IBEX_DAYTONA_OK")])
        read = backend.download_files([f"{workdir}/verification.txt"])
        executed = backend.execute(f"cd {workdir} && python -m pytest -q tests", timeout=60)
        baseline_exit = executed.exit_code
        if executed.exit_code != 0 and "No module named pytest" in executed.output:
            installed = backend.execute("python -m pip install -q pytest fastapi httpx", timeout=120)
            if installed.exit_code == 0:
                executed = backend.execute(f"cd {workdir} && python -m pytest -q tests", timeout=60)
        results["daytona"] = {"sandbox_created": True, "reused_same_sandbox": reuse_ok, "default_snapshot_test_exit": baseline_exit, "file_roundtrip": read[0].content == b"IBEX_DAYTONA_OK",
                              "upload_error": wrote[0].error, "test_exit_code": executed.exit_code,
                              "test_output": executed.output[-1500:]}
    except Exception as error:
        results["daytona"] = {"error_type": type(error).__name__, "message": str(error)[:600]}
    finally:
        if sandbox is not None and client is not None:
            try:
                client.delete(sandbox, timeout=60, wait=True)
                results["daytona"]["resource_deleted"] = True
            except Exception as error:
                results["daytona"]["cleanup_error"] = type(error).__name__
                results["daytona"]["sandbox_id_for_cleanup"] = sandbox.id
    Path("logs/external-features.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    project = None
    try:
        from langsmith import Client, traceable, tracing_context
        import requests
        session = requests.Session()
        session.trust_env = False
        trace_client = Client(api_url=config.get("SYC_LANGSMITH_ENDPOINT"), api_key=config.get("SYC_LANGSMITH_API_KEY"), session=session)
        project_name = "ibex-verification-" + uuid4().hex[:12]
        project = trace_client.create_project(project_name)
        run_id = uuid4()
        @traceable(name="ibex-local-verification", run_type="chain")
        def sample() -> str:
            """Trace a non-sensitive local constant."""
            return "IBEX_TRACE_OK"
        with tracing_context(enabled=True, client=trace_client, project_name=project_name):
            sample(langsmith_extra={"run_id": run_id})
        trace_client.flush()
        import time
        from langsmith.utils import LangSmithNotFoundError
        for attempt in range(20):
            try:
                trace = trace_client.read_run(run_id)
                break
            except LangSmithNotFoundError:
                if attempt == 19:
                    raise
                time.sleep(.5)
        results["langsmith"] = {"trace_found": str(trace.id) == str(run_id), "outputs": trace.outputs}
    except Exception as error:
        results["langsmith"] = {"error_type": type(error).__name__, "message": str(error)[:600]}
    finally:
        if project is not None:
            try:
                trace_client.delete_project(project_id=project.id)
                results["langsmith"]["test_project_deleted"] = True
            except Exception as error:
                results["langsmith"]["cleanup_error"] = type(error).__name__
    Path("logs/external-features.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    # Only sanitized results leave the process.
    for value in config.values():
        if value and len(value) > 20:
            for result in results.values():
                if "message" in result:
                    result["message"] = result["message"].replace(value, "[redacted]")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
