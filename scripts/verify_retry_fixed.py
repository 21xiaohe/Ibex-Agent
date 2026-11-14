"""Exercise production task retries through real Redis/arq on an isolated queue."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from arq import create_pool
from arq.connections import RedisSettings
from infra.settings import get_settings
from scripts.run_local import LocalWorker
from tasks.worker import process_issue


async def main() -> None:
    """Recover on attempt two and remove only this verification's Redis keys."""
    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    tag = "ibex-retry-fix-" + uuid4().hex
    queue = tag + ":queue"
    worker = None
    try:
        job = await redis.enqueue_job("process_issue", {"channel": "verification", "user_id": "test", "text": "test", "conversation_id": tag}, _queue_name=queue, _job_id=tag)
        worker = LocalWorker(functions=[process_issue], redis_pool=redis, queue_name=queue, burst=True,
                             poll_delay=.05, handle_signals=False, max_tries=settings.task_max_tries,
                             ctx={"checkpointer": None, "store": None})
        handler = AsyncMock(side_effect=[ConnectionError("injected transient failure"), "RECOVERED"])
        with patch("tasks.worker.handle_message", handler), patch.object(settings, "task_retry_delay", .05):
            await worker.async_run()
        result = await job.result(timeout=1)
        info = await job.result_info()
        assert result["reply"] == "RECOVERED" and info.job_try == 2 and handler.await_count == 2
        assert handler.await_args.kwargs["message_id"] == tag
        report = {"result": result, "attempts": info.job_try, "handler_calls": handler.await_count, "retried": worker.jobs_retried}
        Path("logs/retry-fixed.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    finally:
        await redis.delete(queue, "arq:job:" + tag, "arq:result:" + tag, "arq:retry:" + tag)
        if worker:
            await worker.close()
        else:
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
