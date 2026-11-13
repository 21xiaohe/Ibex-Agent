"""Private Docker broker; only this service has access to the host Docker socket."""
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import asdict
import hmac
from pathlib import PurePosixPath
import time
import re
from uuid import uuid4

from fastapi import FastAPI, Depends, Header, HTTPException, Response, Request
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from infra.settings import get_settings
from sandbox.docker_manager import create_one_sandbox, create_readonly_sandbox, seed_project, destroy_sandbox

resources: dict[str, dict] = {}
creation_lock = asyncio.Lock()


async def authorize(request: Request, authorization: str = Header("")) -> None:
    """Require a configured broker secret for every resource operation."""
    if request.url.path == "/healthz":
        return
    token = get_settings().sandbox_api_token.get_secret_value()
    if not token or not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Invalid broker credential")


async def _reap() -> None:
    while True:
        await asyncio.sleep(15)
        for key, entry in list(resources.items()):
            if time.monotonic() - entry["used"] > get_settings().sandbox_idle_timeout:
                async with entry["lock"]:
                    if resources.get(key) is entry and time.monotonic() - entry["used"] > get_settings().sandbox_idle_timeout:
                        await destroy_sandbox(entry["backend"])
                        resources.pop(key, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reap idle resources and clean up on shutdown."""
    await cleanup_orphans()
    reaper = asyncio.create_task(_reap())
    try:
        yield
    finally:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        for entry in list(resources.values()):
            await destroy_sandbox(entry["backend"])
        resources.clear()


async def cleanup_orphans() -> None:
    """Reconcile only containers labeled for this single broker deployment."""
    owner = get_settings().sandbox_owner
    if not owner:
        return
    from sandbox.docker_manager import _run
    from sandbox.docker_sandbox import DockerSandbox
    code, output = await _run(["docker", "ps", "-aq", "--filter", "label=ibex.owner=" + owner])
    if code:
        raise RuntimeError("Cannot reconcile broker-owned containers")
    for identifier in output.splitlines():
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            raise RuntimeError("Invalid container identifier")
        await destroy_sandbox(DockerSandbox(identifier))


app = FastAPI(title="Ibex private sandbox broker", lifespan=lifespan, dependencies=[Depends(authorize)])


@app.get("/healthz")
async def health() -> dict:
    """Report readiness after owned orphan reconciliation has finished."""
    return {"status": "ok"}


def _entry(identifier: str) -> dict:
    entry = resources.get(identifier)
    if entry is None:
        raise HTTPException(404, "Sandbox not found")
    entry["used"] = time.monotonic()
    return entry


def _path(path: str, workdir: str) -> str:
    value = PurePosixPath(path)
    if not value.is_absolute() or ".." in value.parts or not value.is_relative_to(PurePosixPath(workdir)):
        raise HTTPException(400, "File path outside workspace")
    return str(value)


async def _create(source: str | None = None) -> dict:
    async with creation_lock:
        # Reserve room for one reviewer per working sandbox.
        if len(resources) >= get_settings().sandbox_pool_size * 2:
            raise HTTPException(429, "Sandbox capacity reached")
        backend = None
        try:
            if source is None:
                backend = await create_one_sandbox()
                await seed_project(backend)
            else:
                entry = _entry(source)
                async with entry["lock"]:
                    backend = await create_readonly_sandbox(entry["backend"])
            identifier = uuid4().hex
            resources[identifier] = {"backend": backend, "used": time.monotonic(), "lock": asyncio.Lock()}
            return {"id": identifier, "workdir": backend.workdir}
        except BaseException:
            if backend is not None:
                await destroy_sandbox(backend)
            raise


@app.post("/sandboxes")
async def create() -> dict:
    """Create a seeded sandbox using server-controlled limits and image."""
    return await _create()


@app.post("/sandboxes/{identifier}/readonly")
async def readonly(identifier: str) -> dict:
    """Snapshot an existing resource for an isolated reviewer."""
    return await _create(identifier)


class CommandInput(BaseModel):
    """A bounded command for an already owned container."""
    command: str = Field(min_length=1, max_length=100_000)
    timeout: int = Field(default=120, ge=1, le=300)


@app.post("/sandboxes/{identifier}/execute")
async def execute(identifier: str, body: CommandInput) -> dict:
    """Execute a command inside a container, never in the broker shell."""
    entry = _entry(identifier)
    async with entry["lock"]:
        return asdict(await entry["backend"].aexecute(body.command, timeout=body.timeout))


class UploadInput(BaseModel):
    """A bounded batch of base64 encoded files."""
    files: list[dict[str, str]] = Field(max_length=500)


@app.post("/sandboxes/{identifier}/upload")
async def upload(identifier: str, body: UploadInput) -> dict:
    """Write only to the owned workspace."""
    entry = _entry(identifier)
    files = []
    size = 0
    for item in body.files:
        try:
            data = base64.b64decode(item["content"], validate=True)
            path = _path(item["path"], entry["backend"].workdir)
        except (ValueError, KeyError):
            raise HTTPException(400, "Invalid file payload") from None
        size += len(data)
        if size > 8_000_000:
            raise HTTPException(413, "Upload too large")
        files.append((path, data))
    async with entry["lock"]:
        return {"files": [asdict(item) for item in await entry["backend"].aupload_files(files)]}


class DownloadInput(BaseModel):
    """Paths to exported workspace artifacts."""
    paths: list[str] = Field(max_length=500)


@app.post("/sandboxes/{identifier}/download")
async def download(identifier: str, body: DownloadInput) -> dict:
    """Export files from the owned workspace."""
    entry = _entry(identifier)
    paths = [_path(path, entry["backend"].workdir) for path in body.paths]
    async with entry["lock"]:
        files = await entry["backend"].adownload_files(paths)
    return {"files": [{"path": item.path, "error": item.error,
                      "content": base64.b64encode(item.content).decode() if item.content is not None else None} for item in files]}


@app.delete("/sandboxes/{identifier}")
async def delete(identifier: str) -> dict:
    """Release only a registered resource; repeated deletion is harmless."""
    entry = resources.get(identifier)
    if entry is not None:
        async with entry["lock"]:
            await destroy_sandbox(entry["backend"])
            resources.pop(identifier, None)
    return {"deleted": True}


@app.get("/metrics")
async def metrics() -> Response:
    """Expose broker-owned sandbox gauges behind the same authentication."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
