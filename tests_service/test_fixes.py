"""Regression tests for the previously broken integrations."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_search_and_html_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real HTTP parsing extracts citations and strips executable page content."""
    from tools import search_tools as module
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("format") == "rss":
            return httpx.Response(200, text='<rss><channel><item><title>Python</title><link>https://docs.python.org</link><description>Decimal</description></item></channel></rss>')
        return httpx.Response(200, text='<html><script>hidden()</script><h1>Decimal docs</h1></html>', headers={"content-type": "text/html"})
    factory = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(module, "_public_url", AsyncMock(return_value="93.184.216.34"))
    result = json.loads(asyncio.run(module.web_search.ainvoke({"query": "Decimal"})))
    assert result["results"][0]["url"] == "https://docs.python.org"
    page = json.loads(asyncio.run(module.fetch_url.ainvoke({"url": "https://docs.python.org"})))
    assert page["text"] == "Decimal docs"


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://169.254.169.254", "file:///etc/passwd", "http://user:pass@example.com", "http://example.com:8000"])
def test_retrieval_rejects_private_urls(url: str) -> None:
    """Reject local metadata, credential-bearing and non-web URLs."""
    from tools.search_tools import _public_url
    with pytest.raises(ValueError):
        asyncio.run(_public_url(url))


@pytest.mark.parametrize("mode", ["ok", "error", "disconnect"])
def test_stream_events_and_cleanup(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Streaming sends real deltas/errors and always releases the owned resource."""
    from api.routes import chat
    async def stream(*args: object, **kwargs: object):
        yield SimpleNamespace(content="first"), {"langgraph_node": "model"}
        if mode == "error":
            raise RuntimeError("injected")
        yield SimpleNamespace(content="second"), {"langgraph_node": "model"}
    sandbox = object()
    monkeypatch.setattr(chat, "build_agent_for_service", AsyncMock(return_value=(SimpleNamespace(astream=stream), sandbox)))
    cleanup = AsyncMock()
    monkeypatch.setattr(chat, "release_backend", cleanup)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(checkpointer=None, store=None)), state=SimpleNamespace(tenant_id="test"), is_disconnected=AsyncMock(return_value=mode == "disconnect"))
    async def run() -> str:
        response = await chat.stream_chat(chat.ChatRequest(text="test"), request)
        return "".join([chunk async for chunk in response.body_iterator])
    data = asyncio.run(run())
    cleanup.assert_awaited_once_with(sandbox)
    if mode == "ok":
        assert '"text": "first"' in data and "[DONE]" in data
    elif mode == "error":
        assert '"error"' in data and "[DONE]" not in data
    else:
        assert not data


@pytest.mark.parametrize("attempt,error,retry", [(1, ConnectionError, True), (3, ConnectionError, False), (1, RuntimeError, False)])
def test_task_retry_bounds(monkeypatch: pytest.MonkeyPatch, attempt: int, error: type, retry: bool) -> None:
    """Retry transient failures, stop at the bound, and leave coding errors visible."""
    from arq import Retry
    from tasks import worker
    monkeypatch.setattr(worker, "handle_message", AsyncMock(side_effect=error("injected")))
    payload = {"channel": "test", "user_id": "test", "text": "test", "conversation_id": "test"}
    with pytest.raises(Retry if retry else error):
        asyncio.run(worker.process_issue({"checkpointer": None, "store": None, "job_try": attempt}, payload))


def test_readonly_snapshot_is_fresh_and_immutable(tmp_path: Path) -> None:
    """Snapshot sees the latest code but cannot mutate it through async backend methods."""
    from deepagents.backends import FilesystemBackend
    from sandbox.readonly import reviewer_snapshot
    (tmp_path / "code.py").write_text("latest", encoding="utf-8")
    (tmp_path / ".daytona").mkdir()
    (tmp_path / ".daytona" / "metadata").write_text("provider metadata")
    async def run() -> None:
        async with reviewer_snapshot(FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True), "/") as snapshot:
            assert (await snapshot.adownload_files(["/code.py"]))[0].content == b"latest"
            assert (await snapshot.adownload_files(["/.daytona/metadata"]))[0].error
            assert (await snapshot.awrite("/new.py", "bad")).error
            assert (await snapshot.aedit("/code.py", "latest", "bad")).error
            assert (await snapshot.aupload_files([("/code.py", b"bad")]))[0].error
    asyncio.run(run())
    assert (tmp_path / "code.py").read_text() == "latest"
    assert not (tmp_path / "new.py").exists()


def test_broker_auth_ownership_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only authenticated callers can create resources; paths and deletion stay scoped."""
    from sandbox import service
    from pydantic import SecretStr
    from infra.settings import get_settings
    monkeypatch.setattr(get_settings(), "sandbox_api_token", SecretStr("test-only"))
    backend = SimpleNamespace(workdir="/home/agent")
    monkeypatch.setattr(service, "create_one_sandbox", AsyncMock(return_value=backend))
    monkeypatch.setattr(service, "seed_project", AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(service, "destroy_sandbox", cleanup)
    with TestClient(service.app) as client:
        assert client.post("/sandboxes").status_code == 401
        client.headers["Authorization"] = "Bearer test-only"
        identifier = client.post("/sandboxes").json()["id"]
        assert client.post(f"/sandboxes/{identifier}/download", json={"paths": ["/etc/passwd"]}).status_code == 400
        assert client.post("/sandboxes/foreign/execute", json={"command": "true"}).status_code == 404
        assert client.delete(f"/sandboxes/{identifier}").status_code == 200
        assert client.delete(f"/sandboxes/{identifier}").status_code == 200
    cleanup.assert_awaited_once_with(backend)


def test_team_reviewer_cannot_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force a reviewer write through the actual parent task dispatch graph."""
    from agent import main
    from subagents import profiles
    from deepagents.backends import FilesystemBackend
    from tests_service.test_agent_mechanisms import ScriptedModel
    from langchain.messages import AIMessage
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    backend.workdir = "/"
    parent = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": "reviewer", "description": "Review"}, "id": "r"}]), AIMessage(content="done")])
    reviewer = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/forbidden", "content": "bad"}, "id": "w"}]), AIMessage(content="", tool_calls=[{"name": "ReviewResult", "args": {"approved": False, "summary": "read only", "issues": [], "security_notes": []}, "id": "v"}])])
    monkeypatch.setattr(main, "create_backend", AsyncMock(return_value=backend))
    monkeypatch.setattr(main, "build_model", lambda: parent)
    monkeypatch.setattr(profiles, "get_model_for_role", lambda role: reviewer)
    async def run() -> None:
        graph, _ = await main.build_agent_for_service(team=True)
        result = await graph.ainvoke({"messages": [{"role": "user", "content": "Review"}]})
        assert result["messages"][-1].content == "done"
        assert any("read only" in str(msg.content) for msg in result["messages"] if msg.type == "tool")
    asyncio.run(run())
    assert not (tmp_path / "forbidden").exists()


def test_failed_destroy_is_visible_and_metric_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed removal keeps the active count; repeated successful deletion cannot underflow."""
    from sandbox import docker_manager as module
    from obs.metrics import SANDBOX_ACTIVE
    backend = SimpleNamespace(container_id="metric-test")
    before = SANDBOX_ACTIVE._value.get()
    module._active_ids.add(backend.container_id)
    SANDBOX_ACTIVE.inc()
    monkeypatch.setattr(module, "_run", AsyncMock(side_effect=[(1, "daemon offline"), (0, "removed"), (0, "already absent")]))
    with pytest.raises(RuntimeError, match="removal failed"):
        asyncio.run(module.destroy_sandbox(backend))
    assert SANDBOX_ACTIVE._value.get() == before + 1
    asyncio.run(module.destroy_sandbox(backend))
    asyncio.run(module.destroy_sandbox(backend))
    assert SANDBOX_ACTIVE._value.get() == before


def test_daytona_provider_and_owned_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service provider switch selects Daytona and deletes the owned provider resource."""
    from sandbox import lifecycle, manager
    from infra.settings import get_settings
    from unittest.mock import Mock
    monkeypatch.setattr(get_settings(), "sandbox_provider", "daytona")
    client = SimpleNamespace(delete=Mock())
    remote = SimpleNamespace(id="test-daytona")
    factory = Mock(return_value=(object(), remote, client, "/home/daytona"))
    monkeypatch.setattr(manager, "get_or_create_sandbox_backend", factory)
    async def run() -> None:
        backend = await lifecycle.create_backend()
        assert backend.id == "test-daytona" and backend.workdir == "/home/daytona"
        await lifecycle.release_backend(backend)
    asyncio.run(run())
    client.delete.assert_called_once_with(remote, timeout=60, wait=True)


def test_load_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scenario makes a real endpoint request only when model work is enabled."""
    from unittest.mock import Mock
    monkeypatch.setenv("LOCUST_SKIP_MONKEY_PATCH", "1")
    from loadtest.locustfile import IbexUser
    user = SimpleNamespace(client=Mock())
    IbexUser.readiness(user)
    user.client.get.assert_called_once_with("/readyz")
    monkeypatch.delenv("IBEX_LOAD_AGENT", raising=False)
    IbexUser.issue(user)
    user.client.post.assert_not_called()
    monkeypatch.setenv("IBEX_LOAD_AGENT", "1")
    IbexUser.issue(user)
    assert user.client.post.call_args.args[0] == "/issues/secure"


def test_skills_appear_in_actual_model_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepAgents discovers both skills and presents their metadata to the model."""
    from agent import main
    from langchain.messages import AIMessage
    from tests_service.test_agent_mechanisms import ScriptedModel
    prompts = []
    class RecordingModel(ScriptedModel):
        """Record the real prompt after middleware has loaded skills."""
        def _generate(self, messages: list, *args: object, **kwargs: object):
            prompts.extend(str(message.content) for message in messages if message.type == "system")
            return super()._generate(messages, *args, **kwargs)
    monkeypatch.setattr(main, "build_model", lambda: RecordingModel(responses=[AIMessage(content="ok")]))
    asyncio.run(main.build_agent().ainvoke({"messages": [{"role": "user", "content": "hello"}]}))
    assert "fastapi-endpoint" in "".join(prompts) and "unit-test" in "".join(prompts)


def test_stream_disconnect_finishes_async_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """An actual ASGI disconnect cancels generation but cannot cancel sandbox cleanup."""
    from api.routes import chat
    async def run() -> None:
        disconnected = asyncio.Event()
        cleaned = []
        async def stream(*args: object, **kwargs: object):
            yield SimpleNamespace(content="first"), {"langgraph_node": "model"}
            await asyncio.sleep(60)
        async def cleanup(resource: object) -> None:
            await asyncio.sleep(.02)
            cleaned.append(resource)
        sandbox = object()
        monkeypatch.setattr(chat, "build_agent_for_service", AsyncMock(return_value=(SimpleNamespace(astream=stream), sandbox)))
        monkeypatch.setattr(chat, "release_backend", cleanup)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(checkpointer=None, store=None)), state=SimpleNamespace(tenant_id="test"), is_disconnected=AsyncMock(return_value=False))
        response = await chat.stream_chat(chat.ChatRequest(text="test"), request)
        async def receive() -> dict:
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message: dict) -> None:
            if message["type"] == "http.response.body":
                disconnected.set()
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), timeout=2)
        assert cleaned == [sandbox]
    asyncio.run(run())
