"""Tenant-owned workspace snapshots and PostgreSQL session locks."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import PurePosixPath
import shlex
from typing import Any, AsyncIterator
import anyio
from infra.settings import get_settings


class WorkspaceBusy(RuntimeError):
    """Another request currently owns this conversation workspace."""


_PREFLIGHT = '''import os,pathlib,sys
root=pathlib.Path(sys.argv[1]);count=total=0
ignored={"__pycache__","node_modules",".git",".venv",".daytona",".cache",".pycache",".pytest_cache"}
def kept(name):
 return name not in ignored and (not name.startswith(".") or name in {".github",".gitignore",".dockerignore"})
for directory,dirs,files in os.walk(root,followlinks=False):
 dirs[:]=[n for n in dirs if kept(n)]
 for name in dirs+[n for n in files if kept(n)]:
  path=pathlib.Path(directory)/name;count+=1
  if path.is_symlink(): raise ValueError("Workspace symlinks are not supported")
  if path.is_file(): total+=path.stat().st_size
  if count>int(sys.argv[2]) or total>int(sys.argv[3]): raise ValueError("Workspace quota exceeded")
'''


async def _preflight(backend: Any) -> None:
    settings = get_settings()
    if hasattr(backend, "aexecute"):
        command = "python -c " + shlex.quote(_PREFLIGHT) + " " + " ".join(shlex.quote(str(value)) for value in (backend.workdir, settings.workspace_max_files, settings.workspace_max_bytes))
        result = await backend.aexecute(command, timeout=30)
        if result.exit_code:
            raise ValueError("Workspace quota or symlink validation failed")
    else:
        # Execute fixed validation code locally against this owned temporary directory.
        import subprocess
        import sys
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", _PREFLIGHT, str(backend.cwd), str(settings.workspace_max_files), str(settings.workspace_max_bytes)], capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("Workspace quota or symlink validation failed")


def _relative(path: str, workdir: str) -> str:
    value = PurePosixPath(path)
    if ".." in value.parts or not value.is_absolute() or not value.is_relative_to(PurePosixPath(workdir)):
        raise ValueError("Workspace path escapes its root")
    return value.relative_to(PurePosixPath(workdir)).as_posix()


async def snapshot_files(backend: Any) -> dict[str, str]:
    """Export bounded project files; omit provider metadata and generated caches."""
    settings = get_settings()
    await _preflight(backend)
    pending, paths, count = [backend.workdir], [], 0
    while pending:
        listing = await backend.als(pending.pop())
        if listing.error:
            raise RuntimeError("Workspace listing failed")
        for item in listing.entries:
            rel = _relative(item["path"], backend.workdir)
            if any(part in {"__pycache__", "node_modules", ".git", ".venv", ".daytona", ".cache", ".pycache", ".pytest_cache"} or (part.startswith(".") and part not in {".github", ".gitignore", ".dockerignore"}) for part in PurePosixPath(rel).parts):
                continue
            count += 1
            if count > settings.workspace_max_files:
                raise ValueError("Workspace file limit exceeded")
            (pending if item.get("is_dir") else paths).append(item["path"])
    files, total = {}, 0
    # Download one at a time so a large aggregate is never buffered before checking.
    for path in paths:
        item = (await backend.adownload_files([path]))[0]
        if item.error or item.content is None:
            raise RuntimeError("Workspace download failed")
        total += len(item.content)
        if total > settings.workspace_max_bytes:
            raise ValueError("Workspace byte limit exceeded")
        files[_relative(path, backend.workdir)] = base64.b64encode(item.content).decode()
    return files


async def restore_files(backend: Any, files: dict[str, str]) -> None:
    """Restore a prior manifest, including deletion of previously seeded files."""
    current = await snapshot_files(backend)
    for path in files:
        if _relative(str(PurePosixPath(backend.workdir) / path), backend.workdir) != path:
            raise ValueError("Invalid stored workspace path")
    deleted = sorted(current.keys() - files.keys())
    if deleted:
        if hasattr(backend, "aexecute"):
            code = "import pathlib,sys,json;root=pathlib.Path(sys.argv[1]);[(root/p).unlink(missing_ok=True) for p in json.loads(sys.argv[2])]"
            command = "python -c " + shlex.quote(code) + " " + shlex.quote(backend.workdir) + " " + shlex.quote(json.dumps(deleted))
            result = await backend.aexecute(command, timeout=30)
            if result.exit_code:
                raise RuntimeError("Workspace deletion restore failed")
        else:
            for path in deleted:
                target = backend.cwd / path
                if not target.resolve().is_relative_to(backend.cwd.resolve()):
                    raise ValueError("Workspace path escapes its root")
                await asyncio.to_thread(target.unlink, missing_ok=True)
    batch = [(str(PurePosixPath(backend.workdir) / name), base64.b64decode(content, validate=True)) for name, content in files.items()]
    results = await backend.aupload_files(batch)
    if len(results) != len(batch) or any(item.error for item in results):
        raise RuntimeError("Workspace restore failed")


class WorkspaceLease:
    """A live database connection owns the lock until save and cleanup finish."""
    def __init__(self, conn: Any, tenant: str, thread: str, files: dict | None) -> None:
        self.conn, self.tenant, self.thread, self.files = conn, tenant, thread, files

    async def restore(self, backend: Any) -> None:
        """Restore only when a prior complete snapshot exists."""
        if self.files is not None:
            await restore_files(backend, self.files)

    async def save(self, backend: Any) -> None:
        """Atomically replace the manifest; an export failure preserves the previous version."""
        if self.conn is None:
            return
        files = await snapshot_files(backend)
        await self.conn.execute(
            "INSERT INTO ibex_workspaces(tenant_id,thread_id,files) VALUES (%s,%s,%s) "
            "ON CONFLICT(tenant_id,thread_id) DO UPDATE SET files=excluded.files,version=ibex_workspaces.version+1,updated_at=now()",
            (self.tenant, self.thread, json.dumps(files)))


@asynccontextmanager
async def workspace_lease(pool: Any, tenant: str, thread: str) -> AsyncIterator[WorkspaceLease]:
    """Serialize a conversation across API and worker processes without expiring leases."""
    if pool is None:
        yield WorkspaceLease(None, tenant, thread, None)
        return
    key = int.from_bytes(hashlib.sha256(json.dumps([tenant, thread]).encode()).digest()[:8], "big", signed=True)
    manager = pool.connection()
    conn = await manager.__aenter__()
    acquired = False
    try:
        cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,))
        acquired = (await cursor.fetchone())["acquired"]
        if not acquired:
            raise WorkspaceBusy("Conversation is already running; retry after it completes")
        cursor = await conn.execute("SELECT files FROM ibex_workspaces WHERE tenant_id=%s AND thread_id=%s", (tenant, thread))
        row = await cursor.fetchone()
        yield WorkspaceLease(conn, tenant, thread, row["files"] if row else None)
    finally:
        with anyio.CancelScope(shield=True):
            try:
                if acquired:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except BaseException:
                # Never return a connection with an uncertain session lock to the pool.
                await conn.close()
                raise
            finally:
                await manager.__aexit__(None, None, None)
