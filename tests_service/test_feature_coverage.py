"""Feature-level regressions using local fakes; external services are not contacted."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.messages import AIMessage, ToolMessage
from langchain.agents.middleware import ModelRequest, ModelResponse


def test_auth_valid_and_invalid() -> None:
    """Configured keys resolve tenants; missing or unknown keys are rejected."""
    from gateway.auth import require_api_key
    from infra.settings import get_settings
    from fastapi import HTTPException
    for key, tenant in get_settings().api_keys.items():
        assert asyncio.run(require_api_key(key)) == tenant
    for key in (None, "invalid-test-key"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(require_api_key(key))
        assert error.value.status_code == 401


def test_session_dimensions() -> None:
    """Normal tenant/channel/conversation dimensions generate distinct sessions."""
    from channels.base import InboundMessage
    from channels.session import thread_id_for
    msg = InboundMessage(channel="api", user_id="test", text="hello", conversation_id="same")
    assert thread_id_for(msg, "a") == thread_id_for(msg, "a")
    assert thread_id_for(msg, "a") != thread_id_for(msg, "b")
    assert thread_id_for(replace(msg, channel="cli"), "a") != thread_id_for(msg, "a")


def test_context_unique_request_ids() -> None:
    """Request identities change while the caller identity is preserved."""
    from middleware.context import RequestContextMiddleware
    middleware = RequestContextMiddleware(user_id="tester", channel="cli")
    first = asyncio.run(middleware.abefore_agent({}, None))
    second = asyncio.run(middleware.abefore_agent({}, None))
    assert first["request_id"] != second["request_id"]
    assert first["user_id"] == "tester" and first["channel"] == "cli"


def test_concurrency_limit_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent calls respect the limit and release permits after errors."""
    import middleware.concurrency as module
    async def run() -> None:
        monkeypatch.setattr(module, "_llm_sem", asyncio.Semaphore(2))
        active = peak = 0
        async def handler(request: object) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.01)
                if request == "fail":
                    raise ValueError("injected")
                return "ok"
            finally:
                active -= 1
        middleware = module.LLMConcurrencyMiddleware()
        results = await asyncio.gather(*(middleware.awrap_model_call(item, handler) for item in ["fail", *range(8)]), return_exceptions=True)
        assert peak == 2 and active == 0
        assert sum(isinstance(item, ValueError) for item in results) == 1
        assert await middleware.awrap_model_call(None, handler) == "ok"
    asyncio.run(run())


def test_cost_accumulates_usage() -> None:
    """Middleware adds model usage to existing conversation totals."""
    from middleware.cost import CostMeterMiddleware
    response = ModelResponse(result=[AIMessage(content="ok", usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})])
    request = SimpleNamespace(state={"total_input_tokens": 5, "total_output_tokens": 2})
    actual = asyncio.run(CostMeterMiddleware().awrap_model_call(request, AsyncMock(return_value=response)))
    assert actual.command.update == {"total_input_tokens": 17, "total_output_tokens": 5}


def test_audit_marks_returned_tool_error() -> None:
    """A ToolMessage with error status must increment the error metric."""
    from middleware.audit import ToolAuditMiddleware
    from obs.metrics import AGENT_TOOL_CALLS
    request = SimpleNamespace(tool_call={"name": "verification_error_tool"})
    before = AGENT_TOOL_CALLS.labels("verification_error_tool", "error")._value.get()
    response = ToolMessage(content="failed", tool_call_id="test", status="error")
    asyncio.run(ToolAuditMiddleware().awrap_tool_call(request, AsyncMock(return_value=response)))
    assert AGENT_TOOL_CALLS.labels("verification_error_tool", "error")._value.get() == before + 1


def test_metrics_use_route_template() -> None:
    """Two job identifiers must share one metric label, including 404 requests."""
    from obs.metrics import PrometheusMiddleware, REQUEST_COUNT
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)
    @app.get("/verification/{item_id}")
    def endpoint(item_id: str) -> dict:
        """Return a harmless test value."""
        return {"id": item_id}
    client = TestClient(app)
    client.get("/verification/a")
    client.get("/verification/b")
    client.get("/missing-verification-a")
    labels = [sample.labels["path"] for metric in REQUEST_COUNT.collect() for sample in metric.samples if sample.name == "http_requests_total"]
    assert "/verification/{item_id}" in labels
    assert "/verification/a" not in labels and "/verification/b" not in labels
    assert "/missing-verification-a" not in labels


def test_pool_releases_slot_when_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed destroy must not permanently exhaust the pool."""
    from sandbox import pool as module
    monkeypatch.setattr(module, "create_one_sandbox", AsyncMock(return_value=SimpleNamespace(workdir="/home/agent")))
    monkeypatch.setattr(module, "seed_project", AsyncMock())
    monkeypatch.setattr(module, "destroy_sandbox", AsyncMock(side_effect=[RuntimeError("cleanup"), None]))
    async def run() -> None:
        pool = module.SandboxPool(1)
        with pytest.raises(RuntimeError, match="cleanup"):
            async with pool.acquire():
                pass
        async with asyncio.timeout(.3):
            async with pool.acquire():
                pass
    asyncio.run(run())


@pytest.mark.parametrize("text,expected", [('```json\n{"score":8,"reason":"ok"}\n```', .8), ('{"score":99}', 1.0), ('not json', 0.0)])
def test_judge_parsing(monkeypatch: pytest.MonkeyPatch, text: str, expected: float) -> None:
    """Judge handles fenced JSON, score bounds and invalid responses."""
    import eval.judge as module
    monkeypatch.setattr(module, "get_model", lambda tier: SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=text))))
    result = asyncio.run(module.judge_answer("test", "test", "test"))
    assert result["score"] == expected


def test_missing_code_is_not_static_success() -> None:
    """Empty artifacts must not be scored as compliant code."""
    from eval.exec_scorer import _static_checks
    assert _static_checks(b"")["has_type_hints"] is False



def test_feishu_parser_text_and_nontext() -> None:
    """Non-text events are ignored even if their payload contains a text key."""
    from channels.feishu_ws import extract_feishu_message
    message = SimpleNamespace(chat_id="test", content='{"text":"hello"}', message_type="text")
    event = SimpleNamespace(event=SimpleNamespace(message=message, sender=None))
    assert extract_feishu_message(event).text == "hello"
    message.message_type = "image"
    assert extract_feishu_message(event) is None



def test_readiness_checks_dependency_failures() -> None:
    """Readiness must fail if Redis is unreachable despite an existing pool."""
    from api.app import readyz
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pg_pool=object(), redis=SimpleNamespace(ping=AsyncMock(side_effect=ConnectionError("down"))))))
    result = asyncio.run(readyz(request))
    assert getattr(result, "status_code", 200) == 503


@pytest.mark.parametrize("path", ["/jobs", "/tasks"])
@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": 123}])
def test_invalid_task_input_is_422(path: str, payload: dict) -> None:
    """Malformed requests fail validation before touching queues or databases."""
    from api.routes import jobs, tasks_bg
    from gateway.limiter import limiter
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(jobs.router)
    app.include_router(tasks_bg.router)
    assert TestClient(app).post(path, json=payload).status_code == 422


def test_cli_configurable_endpoint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The CLI supports the actual local API port and preserves conversation identity."""
    import httpx
    from channels import cli
    calls = []
    def transport(request: httpx.Request) -> httpx.Response:
        """Record the local HTTP request without contacting a server."""
        calls.append(request)
        return httpx.Response(200, json={"reply": "cli-ok"})
    factory = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(transport), **kwargs))
    asyncio.run(cli.ask("test", "hello", "http://127.0.0.1:18700/"))
    assert str(calls[0].url) == "http://127.0.0.1:18700/webhook/generic"
    assert "cli-ok" in capsys.readouterr().out


@pytest.mark.parametrize("exit_code,score", [(0, 1.0), (1, 0.0)])
def test_execution_scorer_uses_bound_sandbox(monkeypatch: pytest.MonkeyPatch, exit_code: int, score: float) -> None:
    """The scorer executes tests against the agent's sandbox and trusts exit status."""
    from eval import exec_scorer
    sandbox = SimpleNamespace(
        workdir="/home/agent",
        aupload_files=AsyncMock(return_value=[SimpleNamespace(error=None)]),
        aexecute=AsyncMock(return_value=SimpleNamespace(exit_code=exit_code, output="1 passed (misleading output)")),
        adownload_files=AsyncMock(return_value=[SimpleNamespace(content=b'def f() -> int:\n    return 1\n')]),
    )
    agent = SimpleNamespace(ainvoke=AsyncMock(return_value={"messages": [AIMessage(content="done")]}))
    factory = AsyncMock(return_value=(agent, sandbox))
    cleanup = AsyncMock()
    monkeypatch.setattr(exec_scorer, "destroy_sandbox", cleanup)
    case = {"id": "test", "issue": "test", "test_code": "assert True", "target_file": "app/test.py"}
    result = asyncio.run(exec_scorer.score_by_execution(factory, case))
    assert result["score"] == score
    factory.assert_awaited_once()
    sandbox.aexecute.assert_awaited_once()
    cleanup.assert_awaited_once_with(sandbox)


def test_eval_repeats_are_independent_and_record_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sampling persists every result and counts errors instead of silently dropping them."""
    from eval import run_eval
    scorer = AsyncMock(side_effect=[{"score": 1.0}, RuntimeError("injected"), {"score": 0.5}])
    result = asyncio.run(run_eval._eval_repeated(None, asyncio.Semaphore(1), {"id": "test", "category": "test"}, scorer))
    assert result["scores"] == [1.0, 0.0, .5]
    assert result["mean"] == .5
    assert result["samples"][1]["error"] == "RuntimeError"


def test_redis_rate_limit_shared_between_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """Independent limiter instances share tenant counts while other tenants retain quota."""
    from uuid import uuid4
    from fastapi import Request
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from gateway.limiter import _tenant_key
    from infra.settings import get_settings
    settings = get_settings()
    tag = uuid4().hex
    keys = {"verify-a1": tag + "-a", "verify-a2": tag + "-a", "verify-b": tag + "-b"}
    monkeypatch.setattr(settings, "api_keys", keys)
    def make_client() -> TestClient:
        """Create a separate application sharing only the Redis storage backend."""
        limiter = Limiter(key_func=_tenant_key, storage_uri=settings.redis_url)
        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        @app.get("/limit-verification")
        @limiter.limit("2/minute")
        async def endpoint(request: Request) -> dict:
            """Avoid model work while exercising actual Redis limit counters."""
            return {"ok": True}
        return TestClient(app)
    one, two = make_client(), make_client()
    assert one.get("/limit-verification", headers={"X-API-Key": "verify-a1"}).status_code == 200
    assert two.get("/limit-verification", headers={"X-API-Key": "verify-a2"}).status_code == 200
    assert one.get("/limit-verification", headers={"X-API-Key": "verify-a1"}).status_code == 429
    assert two.get("/limit-verification", headers={"X-API-Key": "verify-b"}).status_code == 200


@pytest.mark.parametrize("success", [True, False])
def test_feishu_sender_reports_api_failure(success: bool) -> None:
    """The real SDK request builder is exercised without sending a live message."""
    from channels.feishu import FeishuChannel
    sender = FeishuChannel()
    response = SimpleNamespace(success=lambda: success, code=999, msg="test failure")
    create = Mock(return_value=response)
    sender._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=create))))
    if success:
        asyncio.run(sender.send("test-only", "test", message_id="stable-test-delivery"))
    else:
        with pytest.raises(RuntimeError, match="Feishu message rejected"):
            asyncio.run(sender.send("test-only", "test", message_id="stable-test-delivery"))
    request = create.call_args.args[0]
    assert request.request_body.receive_id == "test-only"
    assert request.request_body.uuid == "stable-test-delivery"


def test_readiness_timeout_does_not_wait_for_driver_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slow driver cancellation cannot delay the readiness HTTP response or spawn extra probes."""
    import time
    import api.app as module
    calls = 0
    async def blocked(pool: object, redis: object) -> None:
        nonlocal calls
        calls += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(.15)
            raise
    monkeypatch.setattr(module, "READINESS_TIMEOUT", .02)
    monkeypatch.setattr(module, "_probe_dependencies", blocked)
    async def run() -> None:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pg_pool=object(), redis=object())))
        start = time.monotonic()
        assert (await module.readyz(request)).status_code == 503
        assert (await module.readyz(request)).status_code == 503
        assert time.monotonic() - start < .12
        assert calls == 1
        await asyncio.sleep(.2)
    asyncio.run(run())
