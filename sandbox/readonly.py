"""Invocation-time reviewer snapshots, with no access to the writable workspace."""
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, AsyncIterator

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import WriteResult, EditResult, FileUploadResponse
from sandbox.docker_sandbox import DockerSandbox


class ReadOnlyFiles(FilesystemBackend):
    """Read-only file backend without command execution."""
    def write(self, file_path: str, content: str) -> WriteResult:
        """Reject all writes."""
        return WriteResult(error="permission_denied")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """Reject all edits."""
        return EditResult(error="permission_denied")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Reject uploads into the snapshot."""
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


@asynccontextmanager
async def reviewer_snapshot(source: Any, workdir: str) -> AsyncIterator[Any]:
    """Take a fresh snapshot at review time and release it afterwards."""
    if isinstance(source, DockerSandbox) or hasattr(source, "create_readonly"):
        if isinstance(source, DockerSandbox):
            from sandbox.docker_manager import create_readonly_sandbox
            snapshot = await create_readonly_sandbox(source)
        else:
            import asyncio
            snapshot = await asyncio.to_thread(source.create_readonly)
        from sandbox.lifecycle import release_backend
        try:
            yield snapshot
        finally:
            await release_backend(snapshot)
        return
    with tempfile.TemporaryDirectory(prefix="ibex-review-") as directory:
        pending, paths = [workdir], []
        entries_seen = 0
        while pending:
            listing = await source.als(pending.pop())
            if listing.error:
                raise RuntimeError("Reviewer snapshot listing failed")
            for item in listing.entries:
                entries_seen += 1
                if entries_seen > 500:
                    raise ValueError("Reviewer snapshot exceeds 500 entries")
                path = PurePosixPath(item["path"])
                if ".." in path.parts or not path.is_absolute() or not path.is_relative_to(PurePosixPath(workdir)):
                    raise ValueError("Invalid snapshot path")
                if any(part == "__pycache__" or (part.startswith(".") and part != ".github") for part in path.relative_to(PurePosixPath(workdir)).parts):
                    continue
                (pending if item.get("is_dir") else paths).append(str(path))
        total = 0
        for item in await source.adownload_files(paths):
            if item.error or item.content is None:
                raise RuntimeError("Reviewer snapshot download failed")
            total += len(item.content)
            if total > 8_000_000:
                raise ValueError("Reviewer snapshot exceeds 8 MB")
            target = Path(directory).joinpath(*PurePosixPath(item.path).parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.content)
        yield ReadOnlyFiles(root_dir=directory, virtual_mode=True)
