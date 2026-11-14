"""Verify actual API routes and persisted tenant separation."""
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from channels.base import InboundMessage
from channels.session import thread_id_for
from infra.settings import get_settings
from infra.persistence import open_persistence


async def main() -> None:
    """Write real HTTP and database evidence, excluding authentication headers."""
    base = "http://127.0.0.1:18700"
    results = {}
    conversation = "verify-" + uuid4().hex
    async with httpx.AsyncClient(trust_env=False, timeout=90, headers={"Connection": "close"}) as client:
        for path in ("/healthz", "/readyz", "/docs", "/metrics", "/jobs/not-a-job", "/tasks/not-a-task"):
            response = await client.get(base + path)
            results[path] = {"status": response.status_code}
        for name, path, payload in [
            ("issues", "/issues", {"issue": "Reply exactly ISSUE_OK; no tools.", "thread_id": conversation}),
            ("secure_denied", "/issues/secure", {"issue": "test"}),
            ("missing_job_input", "/jobs", {}), ("empty_task_input", "/tasks", {"text": ""}),
            ("stream", "/chat/stream", {"text": "test"}),
        ]:
            response = await client.post(base + path, json=payload)
            results[name] = {"status": response.status_code, "body": response.text if "text/event-stream" in response.headers.get("content-type", "") else response.json()}
        tenants = list(get_settings().api_keys.items())[:2]
        for index, (key, tenant) in enumerate(tenants):
            response = await client.post(base + "/issues/secure", headers={"X-API-Key": key}, json={
                "issue": f"Reply exactly TENANT_{index}_OK; no tools.", "thread_id": conversation, "user_id": "verification", "channel": "api",
            })
            results[f"secure_tenant_{index}"] = {"status": response.status_code, "body": response.text if "text/event-stream" in response.headers.get("content-type", "") else response.json()}
        response = await client.post(base + "/tasks", json={"text": "Reply exactly BACKGROUND_OK; no tools.", "conversation_id": conversation})
        task_id = response.json()["task_id"]
        results["background_submit"] = {"status": response.status_code, "body": response.text if "text/event-stream" in response.headers.get("content-type", "") else response.json()}
        for attempt in range(120):
            response = await client.get(base + "/tasks/" + task_id)
            data = response.json()
            if data["status"] in ("done", "error"):
                results["background_result"] = data
                break
            await asyncio.sleep(.5)
        cors = await client.options(base + "/webhook/generic", headers={"Origin": "http://127.0.0.1:18701", "Access-Control-Request-Method": "POST"})
        results["cors"] = {"status": cors.status_code, "allow_origin": cors.headers.get("access-control-allow-origin")}
    Path("logs/http-features.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    pool, saver, store = await open_persistence()
    from agent.main import build_agent
    graph = build_agent(checkpointer=saver, store=store)
    try:
        isolated = []
        for index, (_, tenant) in enumerate(tenants):
            message = InboundMessage(channel="api", user_id="verification", text="", conversation_id=conversation)
            state = await graph.aget_state({"configurable": {"thread_id": thread_id_for(message, tenant)}})
            messages = state.values["messages"]
            text = " ".join(str(message.content) for message in messages)
            isolated.append(f"TENANT_{index}_OK" in text and f"TENANT_{1-index}_OK" not in text)
        results["tenant_database_isolation"] = isolated
    finally:
        await pool.close()
    Path("logs/http-features.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
