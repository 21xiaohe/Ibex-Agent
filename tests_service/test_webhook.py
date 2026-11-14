"""Webhook regressions; no model calls or external messages are sent."""
from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from channels import webhook


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Create a route-only app with distinct Redis and arq clients."""
    from infra.settings import get_settings
    monkeypatch.setattr(get_settings(), "feishu_verification_token", "test-token")
    monkeypatch.setattr(get_settings(), "feishu_app_id", "test-app")
    instance = FastAPI()
    @instance.middleware("http")
    async def identity(request, call_next):
        request.state.tenant_id = "test-tenant"
        return await call_next(request)
    instance.state.pg_pool = object()
    instance.include_router(webhook.router)
    instance.state.redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    instance.state.arq_redis = object()
    instance.state.checkpointer = object()
    instance.state.store = object()
    return instance


def event() -> dict:
    """Return a fake Feishu text event used only for local routing tests."""
    return {"header": {"event_id": "local-test-event", "token": "test-token", "app_id": "test-app", "event_type": "im.message.receive_v1"},
            "event": {"message": {"chat_id": "test-only", "message_type": "text", "content": '{"text":"hello"}'},
                      "sender": {"sender_id": {"open_id": "test-user"}}}}


def test_feishu_uses_arq_client(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit through the arq client, not the plain Redis client."""
    enqueue = AsyncMock(return_value="local-test-event")
    monkeypatch.setattr(webhook, "submit_job", enqueue)
    assert TestClient(app).post("/webhook/feishu", json=event()).status_code == 200
    assert enqueue.await_args.args[1] is app.state.arq_redis


def test_failed_enqueue_allows_redelivery(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Release the dedup marker if queue submission fails."""
    monkeypatch.setattr(webhook, "submit_job", AsyncMock(side_effect=RuntimeError("queue down")))
    response = TestClient(app, raise_server_exceptions=False).post("/webhook/feishu", json=event())
    assert response.status_code == 500
    app.state.redis.set.assert_not_awaited()


def test_generic_dispatches_nonempty_text(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise message construction and dispatch through the HTTP route."""
    handler = AsyncMock(return_value="local-ok")
    monkeypatch.setattr(webhook, "handle_message", handler)
    response = TestClient(app).post("/webhook/generic", json={"text": "hello", "source": "test"})
    assert response.status_code == 200
    assert response.json()["reply"] == "local-ok"
    assert handler.await_args.args[0].text == "hello"
