"""Verify the configured Daytona lifecycle wrapper and file-only reviewer snapshot."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from dotenv import dotenv_values
from infra.settings import get_settings
from sandbox.lifecycle import create_backend, release_backend
from sandbox.readonly import reviewer_snapshot


async def verify() -> None:
    """Use an isolated disposable workspace, with no external messages or model calls."""
    get_settings().sandbox_provider = "daytona"
    backend = None
    report = {}
    try:
        backend = await create_backend()
        target = backend.workdir + "/provider-test.txt"
        await backend.aupload_files([(target, b"PROVIDER_OK")])
        read = await backend.adownload_files([target])
        command = await backend.aexecute('python -c "print(17*19)"', timeout=30)
        async with reviewer_snapshot(backend, backend.workdir) as snapshot:
            copied = await snapshot.adownload_files([target])
            denied = await snapshot.awrite(backend.workdir + "/denied.txt", "bad")
            assert copied[0].content == b"PROVIDER_OK" and denied.error
        tested = await backend.aexecute("cd " + backend.workdir + " && python -m pytest -q tests", timeout=60)
        assert tested.exit_code == 0
        report = {"pytest_exit": tested.exit_code, "pytest_output": tested.output, "provider": "daytona", "wrapper": type(backend).__name__, "roundtrip": read[0].content == b"PROVIDER_OK",
                  "command_exit": command.exit_code, "command_output": command.output, "readonly_snapshot": True}
        assert report["roundtrip"] and command.exit_code == 0 and "323" in command.output
    finally:
        if backend:
            await release_backend(backend)
            report["deleted"] = True
        Path("logs/daytona-service-fixed.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Local dotenv file containing DAYTONA_API_KEY and optional DAYTONA_TARGET")
    config = dotenv_values(parser.parse_args().config)
    for key in ("DAYTONA_API_KEY", "DAYTONA_TARGET"):
        if config.get(key):
            os.environ[key] = config[key]
    asyncio.run(verify())
