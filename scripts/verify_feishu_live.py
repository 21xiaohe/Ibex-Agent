"""Listen for one explicit verification token, then send exactly one test reply.

Run only after the user authorizes the live reply. Never responds to other text.
"""
import asyncio
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4

from dotenv import dotenv_values

TOKEN = "IBEX_VERIFY_1113"
REPLY = "Ibex 全链路测试成功：IBEX_FEISHU_OK"


def main() -> None:
    """Use the existing parser, queue, worker and sender for a single approved test."""
    values = dotenv_values("D:/ShanYangClaw/shanyang-claw/.env")
    for suffix in ("APP_ID", "APP_SECRET"):
        os.environ["IBEX_FEISHU_" + suffix] = values["SYC_FEISHU_" + suffix]
    import lark_oapi as lark
    from lark_oapi.ws import client as ws_module
    from channels.feishu_ws import extract_feishu_message
    from channels.feishu import FeishuChannel
    from tasks.queue import get_arq_redis, enqueue_issue, get_job_result

    results = {"state": "waiting", "trigger": TOKEN}
    started = time.monotonic()
    claimed = False
    finished = False
    task = None

    def save() -> None:
        Path("logs/feishu-live.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    async def process(message: object) -> None:
        nonlocal finished
        redis = await get_arq_redis()
        try:
            job_id = await enqueue_issue(redis, {
                "channel": "feishu", "user_id": message.user_id,
                "conversation_id": "verification-" + uuid4().hex,
                "text": "Reply exactly IBEX_FEISHU_OK; do not use tools.",
            })
            results.update(state="processing", job_id=job_id)
            save()
            async with asyncio.timeout(120):
                while True:
                    result = await get_job_result(redis, job_id)
                    if result["status"] == "complete":
                        break
                    await asyncio.sleep(.5)
            if result.get("result", {}).get("reply") != "IBEX_FEISHU_OK":
                raise RuntimeError("Worker did not return expected verification result")
            await FeishuChannel().send(message.conversation_id, REPLY)
            results.update(state="passed", message_accepted_by_feishu=True, reply=REPLY)
        except Exception as error:
            results.update(state="failed", error_type=type(error).__name__, error=str(error)[:400])
        finally:
            await redis.aclose()
            finished = True
            save()

    def receive(event: object) -> None:
        nonlocal claimed, task
        message = extract_feishu_message(event)
        if claimed or message is None or message.text != TOKEN:
            return
        claimed = True
        results.update(state="received", chat_id=message.conversation_id)
        save()
        task = ws_module.loop.create_task(process(message))

    async def bounded_wait() -> None:
        while not finished and time.monotonic() - started < 300:
            await asyncio.sleep(.2)
        if not finished:
            results.update(state="timeout")
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            save()

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(receive).build()
    client = lark.ws.Client(values["SYC_FEISHU_APP_ID"], values["SYC_FEISHU_APP_SECRET"], event_handler=handler, log_level=lark.LogLevel.ERROR)
    save()
    try:
        with patch.object(ws_module, "_select", bounded_wait):
            client.start()
    finally:
        ws_module.loop.run_until_complete(client._disconnect())
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
