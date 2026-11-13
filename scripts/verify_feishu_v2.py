"""One authorized private-chat probe through the real worker and durable outbox.

Run inside the configured deployment only after explicit permission to send the
fixed reply. Other messages are ignored. No credentials or chat IDs are reported.
"""
import asyncio
import json
from pathlib import Path
import time
from typing import Any
from unittest.mock import patch

TOKEN = "IBEX_VERIFY_V2"
REPLY = "IBEX_FEISHU_V2_OK"


def verification_payload(event: Any, tenant: str) -> tuple[dict, str] | None:
    """Accept only the exact private-chat trigger with a stable event identity."""
    from channels.feishu_ws import extract_feishu_message
    from channels.feishu_auth import event_job_id
    if getattr(event.event.message, "chat_type", None) != "p2p":
        return None
    message = extract_feishu_message(event)
    identifier = getattr(getattr(event, "header", None), "event_id", None) or getattr(event.event.message, "message_id", None)
    if message is None or message.text != TOKEN or not identifier:
        return None
    return ({"channel": "feishu", "user_id": message.user_id,
             "conversation_id": message.conversation_id, "tenant_id": tenant,
             "text": f"Reply exactly {REPLY}. No explanation, formatting or tools.",
             "reply_to": {"type": "feishu", "chat_id": message.conversation_id}},
            event_job_id(identifier))


def main() -> None:
    """Bound the listener and observe database completion without sending directly."""
    import lark_oapi as lark
    from lark_oapi.ws import client as ws_module
    from infra.settings import get_settings
    from infra.db import build_pg_pool
    from tasks.queue import get_arq_redis, submit_job
    from tasks.store import get_task
    from tasks.delivery import delivery_status

    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret.get_secret_value():
        raise RuntimeError("Feishu credentials are not configured")
    report: dict = {"state": "waiting", "trigger": TOKEN, "expected_reply": REPLY}
    output = Path("logs/feishu-v2.json")
    output.parent.mkdir(exist_ok=True)
    claimed = False
    task = None
    started = time.monotonic()

    def save() -> None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    async def process(payload: dict, identifier: str) -> None:
        pool = build_pg_pool()
        redis = None
        try:
            await pool.open()
            redis = await get_arq_redis()
            await submit_job(pool, redis, payload, job_id=identifier)
            report.update(state="processing", job_id=identifier)
            save()
            async with asyncio.timeout(240):
                while True:
                    row = await get_task(pool, identifier, payload["tenant_id"])
                    if row and row["status"] == "error":
                        raise RuntimeError("Worker execution failed")
                    if row and row["status"] == "done":
                        result = row["result"]
                        report.update(model_reply_exact=result["reply"] == REPLY,
                                      execution_attempts=row["attempts"])
                        delivery = await delivery_status(pool, result["delivery_id"], payload["tenant_id"])
                        if delivery:
                            report["delivery"] = dict(delivery)
                            save()
                            if delivery["status"] == "dead":
                                raise RuntimeError("Delivery attempts exhausted")
                            if delivery["status"] == "sent":
                                report.update(state="passed" if result["reply"] == REPLY else "unexpected_reply",
                                              provider_accepted=True)
                                break
                    await asyncio.sleep(1)
        except Exception as error:
            report.update(state="failed", error_type=type(error).__name__)
        finally:
            if redis is not None:
                await redis.aclose()
            await pool.close()
            save()

    def receive(event: Any) -> None:
        nonlocal claimed, task
        accepted = verification_payload(event, settings.feishu_tenant_id)
        if claimed or accepted is None:
            return
        claimed = True
        report["state"] = "received"
        save()
        task = ws_module.loop.create_task(process(*accepted))

    async def bounded_wait() -> None:
        while time.monotonic() - started < 900:
            if task is not None and task.done():
                return
            await asyncio.sleep(.25)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        report["state"] = "listener_timeout"
        save()

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(receive).build()
    client = lark.ws.Client(settings.feishu_app_id, settings.feishu_app_secret.get_secret_value(),
                            event_handler=handler, log_level=lark.LogLevel.ERROR)
    save()
    try:
        with patch.object(ws_module, "_select", bounded_wait):
            client.start()
    finally:
        ws_module.loop.run_until_complete(client._disconnect())
    print(json.dumps(report, ensure_ascii=False, default=str), flush=True)
    if report["state"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
