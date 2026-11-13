"""Isolated file-only workspaces for explicitly configured local mode."""
from pathlib import Path
import shutil
import tempfile
from deepagents.backends import FilesystemBackend


class LocalWorkspace(FilesystemBackend):
    """Own a temporary copy rather than exposing the shared service checkout."""
    def __init__(self, source: Path) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ibex-local-")
        root = Path(self.temporary.name)
        for name in ("app", "tests", "skills"):
            if (source / name).is_dir():
                shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
        if (source / "AGENTS.md").is_file():
            shutil.copy2(source / "AGENTS.md", root / "AGENTS.md")
        super().__init__(root_dir=str(root), virtual_mode=True)
        self.workdir = "/"

    def close_remote(self) -> None:
        """Release only this instance's temporary directory."""
        self.temporary.cleanup()
