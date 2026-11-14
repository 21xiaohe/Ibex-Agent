"""Subprocess compatibility for psycopg's Windows event loop."""
import asyncio
import sys
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from sandbox.docker_manager import _run


def test_subprocess_with_database_compatible_loop() -> None:
    """A selector loop must still execute and capture a child process."""
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        code, output = loop.run_until_complete(_run([sys.executable, "-c", "print('child-ok')"]))
    finally:
        loop.close()
    assert code == 0
    assert "child-ok" in output


def test_lifecycle_timeout_reaps_child(tmp_path: Path) -> None:
    """A timed-out real child cannot later write its completion marker."""
    marker = tmp_path / "should-not-exist"
    code = f"import time,pathlib;time.sleep(1);pathlib.Path({str(marker)!r}).write_text('late')"
    async def run() -> None:
        start = time.monotonic()
        with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
            await _run([sys.executable, "-c", code], timeout=.15)
        assert time.monotonic() - start < 2
        await asyncio.sleep(1.1)
        assert not marker.exists()
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses a bounded thread instead of native subprocess cancellation")
def test_cancelled_lifecycle_command_reaps_child(tmp_path: Path) -> None:
    """Cancelling Linux task cleanup also kills the local Docker CLI child."""
    marker = tmp_path / "late"
    code = f"import time,pathlib;time.sleep(1);pathlib.Path({str(marker)!r}).write_text('late')"
    async def run() -> None:
        task = asyncio.create_task(_run([sys.executable, "-c", code], timeout=5))
        await asyncio.sleep(.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.1)
        assert not marker.exists()
    asyncio.run(run())


@pytest.mark.parametrize("result", [(0, "28.3.3"), (1, "offline"), TimeoutError(), subprocess.TimeoutExpired("docker", 3)])
def test_broker_readiness_reflects_docker_failure(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    """A living HTTP broker reports 503 when Docker fails or stops responding."""
    from fastapi import HTTPException
    from sandbox import service, docker_manager
    run = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(docker_manager, "_run", run)
    assert asyncio.run(service.health()) == {"status": "ok"}
    if result == (0, "28.3.3"):
        assert asyncio.run(service.ready()) == {"status": "ready"}
    else:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(service.ready())
        assert raised.value.status_code == 503
        assert raised.value.detail == "Docker dependency unavailable"
    run.assert_awaited_once_with(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=3)
