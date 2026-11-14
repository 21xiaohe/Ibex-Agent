"""One creation and cleanup path for local Docker, broker Docker and Daytona."""
import asyncio
from typing import Any
from uuid import uuid4

from deepagents.backends.sandbox import BaseSandbox
from infra.settings import get_settings


class ManagedDaytonaSandbox(BaseSandbox):
    """Attach lifecycle ownership to the provider's backend."""
    def __init__(self, backend: Any, sandbox: Any, client: Any, workdir: str) -> None:
        self.backend, self.sandbox, self.client, self.workdir = backend, sandbox, client, workdir

    @property
    def id(self) -> str:
        """Return the provider identifier."""
        return self.sandbox.id

    def execute(self, command: str, *, timeout: int | None = None) -> Any:
        """Run a command in the provider workspace."""
        return self.backend.execute(command, timeout=timeout)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list:
        """Upload through the provider API."""
        return self.backend.upload_files(files)

    def download_files(self, paths: list[str]) -> list:
        """Download through the provider API."""
        return self.backend.download_files(paths)


async def create_backend() -> BaseSandbox:
    """Create and initialize exactly the provider selected by configuration."""
    settings = get_settings()
    if settings.sandbox_provider == "docker":
        if settings.sandbox_api_url:
            from sandbox.remote import RemoteDockerSandbox
            return await asyncio.to_thread(RemoteDockerSandbox.create)
        from sandbox.docker_manager import create_one_sandbox, seed_project, destroy_sandbox
        backend = await create_one_sandbox()
        try:
            await seed_project(backend)
            return backend
        except BaseException:
            await destroy_sandbox(backend)
            raise
    if settings.sandbox_provider == "daytona":
        from sandbox.manager import get_or_create_sandbox_backend
        parts = await asyncio.to_thread(get_or_create_sandbox_backend, "task-" + uuid4().hex)
        return ManagedDaytonaSandbox(*parts)
    raise ValueError(f"Unsupported sandbox provider: {settings.sandbox_provider}")


async def release_backend(backend: Any) -> None:
    """Release owned resources without assuming that every backend is Docker."""
    if backend is None:
        return
    if isinstance(backend, ManagedDaytonaSandbox):
        await asyncio.to_thread(backend.client.delete, backend.sandbox, timeout=60, wait=True)
    elif hasattr(backend, "close_remote"):
        await asyncio.to_thread(backend.close_remote)
    else:
        from sandbox.docker_manager import destroy_sandbox
        await destroy_sandbox(backend)
