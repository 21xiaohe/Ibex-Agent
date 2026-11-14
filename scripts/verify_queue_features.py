"""Probe real Redis/arq behavior using a separate queue and controlled task failures."""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from arq import create_pool, Retry
from arq.connections import RedisSettings
from arq.jobs import Job
from infra.idempotency import seen_before
from infra.settings import get_settings
from scripts.run_local import LocalWorker
from tasks.queue import enqueue_issue
from tasks.worker import process_issue


async def retry_example(ctx: dict) -> str:
    """Demonstrate explicit arq Retry as a control for the production failure case."""
    if ctx["job_try"] == 1:
        raise Retry(defer=.05)
    return "EXPLICIT_RETRY_OK"


async def main() -> None:
    """Persist observations and delete only this run's temporary Redis keys."""
    redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    prefix = "ibex-verification-" + uuid4().hex
    queue = prefix + ":queue"
    results = {}
    worker = None
    job_ids = []
    try:
        hits = await asyncio.gather(*(seen_before(redis, prefix, ttl_seconds=60) for _ in range(30)))
        results["idempotency"] = {"first_seen": hits.count(False), "duplicates": hits.count(True)}
        class QueueProxy:
            """Route the existing producer to an isolated verification queue."""
            async def enqueue_job(self, *args: object, **kwargs: object) -> object:
                """Preserve the producer contract while changing only queue identity."""
                return await redis.enqueue_job(*args, **kwargs, _queue_name=queue)
        payload = {"channel": "verification", "user_id": "test", "text": "test", "conversation_id": prefix}
        job_id = prefix + ":normal-failure"
        first = await enqueue_issue(QueueProxy(), payload, job_id=job_id)
        second = await enqueue_issue(QueueProxy(), payload, job_id=job_id)
        job_ids.append(job_id)
        results["queue_dedup"] = {"same_job_id": first == second, "queued_entries": await redis.zcard(queue)}
        control = await redis.enqueue_job("retry_example", _job_id=prefix + ":retry", _queue_name=queue)
        job_ids.append(control.job_id)
        worker = LocalWorker(functions=[process_issue, retry_example], redis_pool=redis, queue_name=queue,
                             burst=True, poll_delay=.1, handle_signals=False, ctx={"checkpointer": None, "store": None})
        handler = AsyncMock(side_effect=RuntimeError("injected upstream failure"))
        with patch("tasks.worker.handle_message", handler):
            await worker.async_run()
        info = await Job(job_id, redis).result_info()
        results["production_exception"] = {"handler_calls": handler.await_count, "success": info.success,
                                           "job_try": info.job_try, "error_type": type(info.result).__name__}
        results["explicit_retry_control"] = {"result": await control.result(timeout=1), "job_try": (await control.result_info()).job_try}
        results["worker_counts"] = {"complete": worker.jobs_complete, "failed": worker.jobs_failed, "retried": worker.jobs_retried}
    finally:
        keys = ["idem:" + prefix, queue]
        for job_id in job_ids:
            keys.extend(["arq:job:" + job_id, "arq:result:" + job_id, "arq:retry:" + job_id])
        await redis.delete(*keys)
        if worker is not None:
            await worker.close()
        else:
            await redis.aclose()
    Path("logs/queue-features.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
