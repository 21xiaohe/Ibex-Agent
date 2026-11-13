"""Real PostgreSQL regressions for tenant ownership, file persistence and delivery recovery."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from infra.db import build_pg_pool
from infra.durable import init_durable_tables
from infra.settings import get_settings
from sandbox.local import LocalWorkspace
from sandbox.workspace import workspace_lease, WorkspaceBusy
from tasks.store import create_task, get_task


def db_case(function) -> None:
    """Run against real service tables and clean up only this test's tenant records."""
    async def run() -> None:
        pool = build_pg_pool()
        await pool.open()
        tenant = "durable-test-" + uuid4().hex
        try:
            await init_durable_tables(pool)
            await function(pool, tenant)
        finally:
            async with pool.connection() as conn:
                for table in ("ibex_outbox", "ibex_workspaces", "async_tasks"):
                    await conn.execute(f"DELETE FROM {table} WHERE tenant_id=%s", (tenant,))
            await pool.close()
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(run())


@pytest.mark.parametrize("path,method,payload", [
    ("/issues", "post", {"issue": "test"}), ("/issues/secure", "post", {"issue": "test"}),
    ("/jobs", "post", {"text": "test"}), ("/tasks", "post", {"text": "test"}),
    ("/webhook/generic", "post", {"text": "test"}), ("/chat/stream", "post", {"text": "test"}),
    ("/jobs/secret", "get", None), ("/tasks/secret", "get", None),
    ("/deliveries/secret", "get", None), ("/deliveries/secret/retry", "post", {}), ("/metrics", "get", None),
])
def test_every_execution_and_result_route_requires_auth(path: str, method: str, payload: dict | None) -> None:
    """The actual application denies access before touching any database, queue or model."""
    from api.app import app
    client = TestClient(app)
    for headers in ({}, {"X-API-Key": "not-a-configured-key"}):
        assert client.request(method, path, json=payload, headers=headers).status_code == 401


def test_workspace_survives_new_backend_and_preserves_deletions(tmp_path: Path) -> None:
    """A new process/backend sees saved bytes; deleted seed files stay deleted."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "seed.py").write_text("seed")
    async def scenario(pool, tenant) -> None:
        backend = LocalWorkspace(tmp_path)
        try:
            async with workspace_lease(pool, tenant, "conversation") as lease:
                await lease.restore(backend)
                await backend.aupload_files([("/app/new.bin", b"\x00\xffpersisted")])
                (backend.cwd / "app" / "seed.py").unlink()
                await lease.save(backend)
                with pytest.raises(WorkspaceBusy):
                    async with workspace_lease(pool, tenant, "conversation"):
                        pass
        finally:
            backend.close_remote()
        restored = LocalWorkspace(tmp_path)
        try:
            async with workspace_lease(pool, tenant, "conversation") as lease:
                await lease.restore(restored)
                assert (await restored.adownload_files(["/app/new.bin"]))[0].content == b"\x00\xffpersisted"
                assert (await restored.adownload_files(["/app/seed.py"]))[0].error
            async with workspace_lease(pool, "foreign-" + tenant, "conversation") as lease:
                assert lease.files is None
        finally:
            restored.close_remote()
    db_case(scenario)


def test_snapshot_failure_preserves_last_good_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Oversized updates cannot silently replace a previously persisted workspace."""
    async def scenario(pool, tenant) -> None:
        backend = LocalWorkspace(tmp_path)
        try:
            async with workspace_lease(pool, tenant, "bounded") as lease:
                await backend.aupload_files([("/ok", b"ok")])
                await lease.save(backend)
                monkeypatch.setattr(get_settings(), "workspace_max_bytes", 3)
                await backend.aupload_files([("/oversized", b"too big")])
                with pytest.raises(ValueError):
                    await lease.save(backend)
            async with workspace_lease(pool, tenant, "bounded") as lease:
                assert set(lease.files) == {"ok"}
        finally:
            backend.close_remote()
    db_case(scenario)


def test_job_and_delivery_queries_hide_other_tenants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a known identifier cannot reveal or resend another tenant's resources."""
    from api.app import app
    from tasks.delivery import finish_task
    from api.routes import jobs, deliveries
    async def scenario(pool, tenant) -> None:
        identifier = uuid4().hex
        await create_task(pool, identifier, "test", {"tenant_id": tenant})
        result = await finish_task(pool, identifier, tenant, {"reply": "private"}, {"type": "feishu", "chat_id": "fake-only"})
        owner_request = SimpleNamespace(state=SimpleNamespace(tenant_id=tenant), app=SimpleNamespace(state=SimpleNamespace(pg_pool=pool)))
        foreign_request = SimpleNamespace(state=SimpleNamespace(tenant_id="other"), app=owner_request.app)
        assert (await jobs.query_job(identifier, owner_request))["result"]["reply"] == "private"
        from fastapi import HTTPException
        for function, value in ((jobs.query_job, identifier), (deliveries.inspect_delivery, result["delivery_id"]), (deliveries.retry, result["delivery_id"])):
            with pytest.raises(HTTPException) as error:
                await function(value, foreign_request)
            assert error.value.status_code == 404
    db_case(scenario)


def test_delivery_retry_exhaustion_replay_and_cached_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending failures retry independently; duplicate jobs reuse the committed model result."""
    from tasks import delivery, worker
    async def scenario(pool, tenant) -> None:
        identifier = uuid4().hex
        payload = {"tenant_id": tenant, "channel": "test", "user_id": "tester", "conversation_id": identifier, "text": "test", "reply_to": {"type": "feishu", "chat_id": "fake-only"}}
        handler = AsyncMock(return_value="cached answer")
        sender = AsyncMock(side_effect=[ConnectionError("injected"), ConnectionError("injected"), None])
        monkeypatch.setattr(worker, "handle_message", handler)
        monkeypatch.setattr(delivery, "send_delivery", sender)
        monkeypatch.setattr(get_settings(), "delivery_max_attempts", 2)
        monkeypatch.setattr(get_settings(), "delivery_retry_delay", 0)
        ctx = {"pg_pool": pool, "tenant_id": tenant, "job_id": identifier, "checkpointer": None, "store": None}
        first = await worker.process_issue(ctx, payload)
        assert sender.await_count == 0
        assert await worker.process_issue(ctx, payload) == first
        assert handler.await_count == 1
        await delivery.deliver_pending(ctx)
        state = await delivery.delivery_status(pool, first["delivery_id"], tenant)
        assert state["status"] == "dead" and state["attempts"] == 2
        assert await delivery.retry_delivery(pool, first["delivery_id"], tenant)
        await delivery.deliver_pending(ctx)
        state = await delivery.delivery_status(pool, first["delivery_id"], tenant)
        assert state["status"] == "sent" and handler.await_count == 1
        assert len({call.args[2] for call in sender.await_args_list}) == 1
    db_case(scenario)


def test_delivery_lease_recovers_after_worker_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restarted sender reclaims expired work while respecting the retry bound."""
    from tasks import delivery
    async def scenario(pool, tenant) -> None:
        identifier = uuid4().hex
        await create_task(pool, identifier, "test", {"tenant_id": tenant})
        result = await delivery.finish_task(pool, identifier, tenant, {"reply": "test"}, {"type": "feishu", "chat_id": "fake-only"})
        async with pool.connection() as conn:
            await conn.execute("UPDATE ibex_outbox SET status='sending',attempts=1,lease_until=now()-interval '1 second' WHERE delivery_id=%s", (result["delivery_id"],))
        sender = AsyncMock()
        monkeypatch.setattr(delivery, "send_delivery", sender)
        await delivery.deliver_pending({"pg_pool": pool, "tenant_id": tenant})
        sender.assert_awaited_once()
        assert (await delivery.delivery_status(pool, result["delivery_id"], tenant))["status"] == "sent"
    db_case(scenario)


def test_db_task_survives_queue_publish_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep republishes a persisted request after the queue connection recovers."""
    from tasks import queue
    monkeypatch.setattr(queue, "clear_failed_result", AsyncMock())
    async def scenario(pool, tenant) -> None:
        producer = AsyncMock(side_effect=ConnectionError("queue down"))
        monkeypatch.setattr(queue, "enqueue_issue", producer)
        payload = {"channel": "test", "tenant_id": tenant, "text": "persisted", "user_id": "test", "conversation_id": "test"}
        identifier = await queue.submit_job(pool, object(), payload)
        assert (await get_task(pool, identifier, tenant))["status"] == "pending"
        async with pool.connection() as conn:
            await conn.execute("UPDATE async_tasks SET updated_at=now()-interval '1 minute' WHERE task_id=%s", (identifier,))
        producer.side_effect = None
        producer.return_value = identifier
        await queue.recover_jobs({"pg_pool": pool, "redis": object()})
        assert producer.await_args.kwargs["job_id"] == identifier
        assert (await get_task(pool, identifier, tenant))["status"] == "queued"
    db_case(scenario)


@pytest.mark.parametrize("success", [True, False])
def test_recovery_clears_only_failed_arq_results(monkeypatch: pytest.MonkeyPatch, success: bool) -> None:
    """Retained timeout failures can be retried, while completed queue results stay intact."""
    from tasks import queue
    monkeypatch.setattr(queue, "Job", lambda *args: SimpleNamespace(result_info=AsyncMock(return_value=SimpleNamespace(success=success))))
    redis = SimpleNamespace(delete=AsyncMock())
    asyncio.run(queue.clear_failed_result(redis, "test-id"))
    if success:
        redis.delete.assert_not_awaited()
    else:
        redis.delete.assert_awaited_once_with("arq:result:test-id", "arq:retry:test-id")


def test_feishu_signature_token_and_age(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callback authentication precedes dedup/queue work; a replayed old signature fails."""
    from channels.feishu_auth import verified_event
    from fastapi import HTTPException
    settings = get_settings()
    monkeypatch.setattr(settings, "feishu_verification_token", "verification-token")
    monkeypatch.setattr(settings, "feishu_encrypt_key", "encryption-key")
    monkeypatch.setattr(settings, "feishu_app_id", "test-app")
    raw = json.dumps({"header": {"token": "verification-token", "app_id": "test-app"}}).encode()
    for age, valid in ((0, True), (1000, False)):
        stamp, nonce = str(int(time.time()) - age), "test-nonce"
        signature = hashlib.sha256((stamp + nonce + "encryption-key").encode() + raw).hexdigest()
        request = SimpleNamespace(body=AsyncMock(return_value=raw), headers={"X-Lark-Request-Timestamp": stamp, "X-Lark-Request-Nonce": nonce, "X-Lark-Signature": signature})
        if valid:
            assert asyncio.run(verified_event(request))["header"]["app_id"] == "test-app"
        else:
            with pytest.raises(HTTPException):
                asyncio.run(verified_event(request))


def test_session_delimiters_do_not_alias_tenants() -> None:
    """Structured identity encoding prevents colon-delimited collisions."""
    from channels.session import thread_id_for
    from channels.base import InboundMessage
    assert thread_id_for(InboundMessage("b:c", "u", "x", "d"), "a") != thread_id_for(InboundMessage("c", "u", "x", "d"), "a:b")


def test_body_cannot_override_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """An attacker-supplied tenant field never enters the execution context."""
    from api.app import app
    from api.routes import issues
    monkeypatch.setattr(get_settings(), "api_keys", {"test-auth-a": "tenant-a"})
    monkeypatch.setattr(app.state, "checkpointer", None, raising=False)
    monkeypatch.setattr(app.state, "store", None, raising=False)
    monkeypatch.setattr(app.state, "pg_pool", object(), raising=False)
    handler = AsyncMock(return_value="ok")
    monkeypatch.setattr(issues, "handle_message", handler)
    response = TestClient(app).post("/issues", json={"issue": "test", "tenant_id": "tenant-b"}, headers={"X-API-Key": "test-auth-a"})
    assert response.status_code == 200
    assert handler.await_args.kwargs["tenant_id"] == "tenant-a"


def test_metrics_token_cannot_execute_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Prometheus credential authorizes metrics only."""
    from api.app import app
    from pydantic import SecretStr
    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr("metrics-only"))
    client = TestClient(app, headers={"Authorization": "Bearer metrics-only"})
    assert client.get("/metrics").status_code == 200
    assert client.post("/issues", json={"issue": "test"}).status_code == 401


def test_owned_orphan_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery asks Docker for this deployment's label and removes only returned IDs."""
    from sandbox import service, docker_manager
    identifier = "a" * 64
    monkeypatch.setattr(get_settings(), "sandbox_owner", "test-deployment")
    run = AsyncMock(return_value=(0, identifier))
    cleanup = AsyncMock()
    monkeypatch.setattr(docker_manager, "_run", run)
    monkeypatch.setattr(service, "destroy_sandbox", cleanup)
    asyncio.run(service.cleanup_orphans())
    assert run.await_args.args[0][-1] == "label=ibex.owner=test-deployment"
    assert cleanup.await_args.args[0].container_id == identifier


def test_daytona_dependencies_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unprepared snapshots either receive explicit initialization or fail before model work."""
    from sandbox.manager import prepare_dependencies
    from unittest.mock import Mock
    backend = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(exit_code=1)))
    monkeypatch.setattr(get_settings(), "daytona_prepare_dependencies", False)
    with pytest.raises(RuntimeError, match="snapshot lacks"):
        prepare_dependencies(backend)
    monkeypatch.setattr(get_settings(), "daytona_prepare_dependencies", True)
    backend.execute.side_effect = [SimpleNamespace(exit_code=1), SimpleNamespace(exit_code=0), SimpleNamespace(exit_code=0)]
    prepare_dependencies(backend)
    assert "pytest==9.1.1" in backend.execute.call_args_list[-2].args[0]


def test_prepare_deploy_does_not_overwrite_existing_secrets(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Secret files match configuration and repeated setup preserves established credentials."""
    from scripts.prepare_deploy import prepare
    from dotenv import dotenv_values
    prepare(tmp_path)
    before = dotenv_values(tmp_path / ".env")
    prepare(tmp_path)
    assert dotenv_values(tmp_path / ".env") == before
    assert (tmp_path / "deploy/secrets/sandbox_token").read_text() == before["IBEX_SANDBOX_API_TOKEN"]
    assert all(value not in capsys.readouterr().out for value in before.values())


def test_fetch_pins_dns_and_revalidates_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second hostname resolution cannot redirect the socket to an unchecked address."""
    import httpx
    from tools import search_tools
    calls = []
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    resolver = AsyncMock(side_effect=["93.184.216.34", ValueError("private address")])
    monkeypatch.setattr(search_tools, "_public_url", resolver)
    factory = httpx.AsyncClient
    monkeypatch.setattr(search_tools.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs))
    result = json.loads(asyncio.run(search_tools.fetch_url.ainvoke({"url": "https://example.com/public"})))
    assert "error" in result and len(calls) == 1
    assert calls[0].url.host == "93.184.216.34"
    assert calls[0].headers["Host"] == "example.com"
    assert calls[0].extensions["sni_hostname"] == "example.com"
