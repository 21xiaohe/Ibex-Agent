"""tasks/queue.py —— 任务入队 + 结果查询（producer 侧，方案二）"""
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus
from datetime import datetime, timezone
from infra.settings import get_settings
from typing import Any
from uuid import uuid4
from tasks.store import create_task, get_task


async def get_arq_redis():
    """建 arq 的 Redis 连接池（producer 用）。lifespan 建一次复用。"""
    return await create_pool(RedisSettings.from_dsn(get_settings().redis_url))


async def enqueue_issue(redis, payload: dict, job_id: str | None = None) -> str:
    """入队，返回 job_id。在 payload 里塞 enqueued_at，供 worker 算排队等待时间。"""
    payload = {**payload, "enqueued_at": datetime.now(timezone.utc).isoformat()}  # 自己塞时间戳
    job = await redis.enqueue_job("process_issue", payload, _job_id=job_id)
    return job.job_id if job is not None else job_id


async def get_job_result(redis, job_id: str) -> dict:
    """查任务状态/结果。"""
    job = Job(job_id=job_id, redis=redis)
    status = await job.status()
    out = {"job_id": job_id, "status": status.value if hasattr(status, "value") else str(status)}
    if status == JobStatus.complete:
        try:
            out["result"] = await job.result(timeout=1)
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
    return out


async def submit_job(pool: Any, redis: Any, payload: dict, job_id: str | None = None) -> str:
    """Persist before publishing; a worker sweep recovers publication failures."""
    identifier = job_id or uuid4().hex
    if not payload.get("tenant_id"):
        raise ValueError("A trusted tenant identity is required")
    await create_task(pool, identifier, payload["channel"], payload)
    if await get_task(pool, identifier, payload["tenant_id"]) is None:
        raise ValueError("Job identity already belongs to another tenant")
    try:
        await enqueue_issue(redis, payload, job_id=identifier)
    except Exception:
        # The durable pending row is the source of truth; no acknowledged work is lost.
        return identifier
    async with pool.connection() as conn:
        await conn.execute("UPDATE async_tasks SET status='queued',updated_at=now() WHERE task_id=%s AND status='pending'", (identifier,))
    return identifier


async def recover_jobs(ctx: dict) -> None:
    """Republish interrupted submissions or expired executions with the same job identity."""
    settings = get_settings()
    async with ctx["pg_pool"].connection() as conn:
        cursor = await conn.execute(
            "SELECT task_id,payload FROM async_tasks WHERE tenant_id<>'__legacy_unowned__' AND "
            "((status IN ('pending','queued') AND updated_at<now()-interval '30 seconds') OR "
            "(status='running' AND updated_at<now()-%s*interval '1 second')) ORDER BY updated_at LIMIT 100",
            (settings.task_timeout + 60,))
        rows = await cursor.fetchall()
    for row in rows:
        await clear_failed_result(ctx["redis"], row["task_id"])
        await submit_job(ctx["pg_pool"], ctx["redis"], row["payload"], row["task_id"])


async def clear_failed_result(redis: Any, identifier: str) -> None:
    """A retained arq timeout result must not block replay of an unfinished database task."""
    info = await Job(identifier, redis).result_info()
    if info is not None and not info.success:
        await redis.delete("arq:result:" + identifier, "arq:retry:" + identifier)
