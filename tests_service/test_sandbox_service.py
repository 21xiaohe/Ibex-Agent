"""Regression coverage for service sandbox construction and file transfer."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent import main
from sandbox import docker_manager, docker_sandbox


@pytest.mark.parametrize("failure", [True, False])
def test_service_builder_cleanup(monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    """Failed graph construction releases its owned backend."""
    sandbox = SimpleNamespace(workdir="/home/agent")
    monkeypatch.setattr(main, "create_backend", AsyncMock(return_value=sandbox))
    cleanup = AsyncMock()
    monkeypatch.setattr(main, "release_backend", cleanup)
    monkeypatch.setattr(main.MCPManager, "from_settings", lambda: SimpleNamespace(get_tools=AsyncMock(return_value=[])))
    build = Mock(side_effect=RuntimeError("build") if failure else None)
    monkeypatch.setattr(main, "_build", build)
    if failure:
        with pytest.raises(RuntimeError, match="build"):
            asyncio.run(main.build_agent_for_service())
        cleanup.assert_awaited_once_with(sandbox)
    else:
        _, actual = asyncio.run(main.build_agent_for_service())
        assert actual is sandbox
        cleanup.assert_not_awaited()


def test_lifecycle_seed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed seed must destroy the just-created Docker resource."""
    from sandbox.lifecycle import create_backend
    from infra.settings import get_settings
    monkeypatch.setattr(get_settings(), "sandbox_api_url", "")
    sandbox = object()
    monkeypatch.setattr(docker_manager, "create_one_sandbox", AsyncMock(return_value=sandbox))
    monkeypatch.setattr(docker_manager, "seed_project", AsyncMock(side_effect=RuntimeError("seed")))
    cleanup = AsyncMock()
    monkeypatch.setattr(docker_manager, "destroy_sandbox", cleanup)
    with pytest.raises(RuntimeError, match="seed"):
        asyncio.run(create_backend())
    cleanup.assert_awaited_once_with(sandbox)


def test_seed_propagates_transfer_failure(tmp_path: Path) -> None:
    """Do not report successful seeding when a file could not be written."""
    (tmp_path / "AGENTS.md").write_text("test", encoding="utf-8")
    sandbox = SimpleNamespace(workdir="/home/agent", container_id="test", upload_files=Mock(return_value=[SimpleNamespace(error="permission_denied")]))
    with pytest.raises(RuntimeError, match="initialization failed"):
        asyncio.run(docker_manager.seed_project(sandbox, root=tmp_path))


def test_upload_uses_container_user_and_binary_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve bytes without docker cp or elevated privileges."""
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(docker_sandbox.subprocess, "run", run)
    sandbox = docker_sandbox.DockerSandbox("test")
    content = b"\x00\xff\n"
    result = sandbox.upload_files([("/home/agent/test.bin", content)])
    assert result[0].error is None
    assert run.call_args.args[0][:4] == ["docker", "exec", "-i", "test"]
    assert run.call_args.kwargs["input"] == content


@pytest.mark.parametrize("code,error", [(0, None), (2, "file_not_found"), (1, "permission_denied")])
def test_download_preserves_bytes_and_errors(monkeypatch: pytest.MonkeyPatch, code: int, error: str | None) -> None:
    """Binary artifacts and missing-file failures must be distinguishable."""
    content = b"\x00\xff\n"
    monkeypatch.setattr(docker_sandbox.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=code, stdout=content)))
    result = docker_sandbox.DockerSandbox("test").download_files(["/home/agent/test.bin"])[0]
    assert result.error == error
    assert result.content == (content if error is None else None)
