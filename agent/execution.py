"""Common persisted execution scope for streaming and ordinary channels."""
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable
import anyio
from agent.main import build_agent_for_service
from sandbox.lifecycle import release_backend
from sandbox.workspace import workspace_lease


@asynccontextmanager
async def execution_session(tenant: str, thread: str, checkpointer: Any, store: Any,
                            pool: Any = None, user_id: str = "anonymous", channel: str = "api",
                            team: bool | None = None, factory: Callable = build_agent_for_service,
                            cleanup: Callable = release_backend) -> AsyncIterator[tuple[Any, Any]]:
    """Restore before execution, persist partial edits on failure, and release owned resources."""
    async with workspace_lease(pool, tenant, thread) as lease:
        backend = None
        restored = False
        try:
            with anyio.CancelScope(shield=True):
                graph, backend = await factory(checkpointer=checkpointer, store=store, user_id=user_id, channel=channel, team=team)
                if backend is not None:
                    await lease.restore(backend)
                restored = True
            yield graph, backend
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if backend is not None and restored:
                        await lease.save(backend)
                finally:
                    await cleanup(backend)
