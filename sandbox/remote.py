"""Sandbox backend for applications without access to a Docker daemon."""
import base64
from typing import Any
import httpx
from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse, FileDownloadResponse
from infra.settings import get_settings


class RemoteDockerSandbox(BaseSandbox):
    """Call an authenticated broker that owns and limits Docker containers."""
    def __init__(self, identifier: str, workdir: str, url: str, token: str) -> None:
        self.identifier, self.workdir, self.url, self.token = identifier, workdir, url.rstrip("/"), token

    @property
    def id(self) -> str:
        """Return the opaque broker resource identifier."""
        return self.identifier

    def _request(self, method: str, suffix: str, payload: Any = None, timeout: float = 90) -> dict:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.request(method, self.url + suffix, headers={"Authorization": "Bearer " + self.token}, json=payload)
            response.raise_for_status()
            return response.json()

    @classmethod
    def create(cls) -> "RemoteDockerSandbox":
        """Ask the broker to create and seed an isolated workspace."""
        s = get_settings()
        initial = cls("", "", s.sandbox_api_url, s.sandbox_api_token.get_secret_value())
        data = initial._request("POST", "/sandboxes")
        return cls(data["id"], data["workdir"], initial.url, initial.token)

    def create_readonly(self) -> "RemoteDockerSandbox":
        """Create a separate immutable reviewer snapshot."""
        data = self._request("POST", f"/sandboxes/{self.id}/readonly")
        return type(self)(data["id"], data["workdir"], self.url, self.token)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute only inside the broker-owned container."""
        seconds = min(timeout or 120, 300)
        data = self._request("POST", f"/sandboxes/{self.id}/execute", {"command": command, "timeout": seconds}, seconds + 15)
        return ExecuteResponse(**data)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Transfer binary files without sharing host directories."""
        payload = {"files": [{"path": path, "content": base64.b64encode(content).decode()} for path, content in files]}
        return [FileUploadResponse(**item) for item in self._request("POST", f"/sandboxes/{self.id}/upload", payload)["files"]]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Retrieve artifacts through the broker."""
        result = self._request("POST", f"/sandboxes/{self.id}/download", {"paths": paths})
        return [FileDownloadResponse(path=item["path"], error=item["error"],
                content=base64.b64decode(item["content"]) if item["content"] is not None else None) for item in result["files"]]

    def close_remote(self) -> None:
        """Idempotently release the broker-owned resource."""
        self._request("DELETE", f"/sandboxes/{self.id}")
