# tasks/worker.py —— on_startup 起指标端口 + 任务埋点
import asyncio
import sys
import time
from datetime import datetime, timezone
from arq import Retry
from sandbox.workspace import WorkspaceBusy
from infra.settings import get_settings
import httpx
from openai import APIConnectionError, APITimeoutError, RateLimitError, InternalServerError

from prometheus_client import start_http_server

from channels.base import InboundMessage
from channels.handler import handle_message
from infra.persistence import open_persistence
from infra.logging import get_logger
from obs.metrics import AGENT_TASK_DURATION, AGENT_QUEUE_WAIT, AGENT_QUEUE_LENGTH

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = get_logger()


async def process_issue(ctx: dict, payload: dict) -> dict:
    """Retry transient execution failures with bounded backoff; never retry a delivered reply."""
    # 排队等待：用入队时塞进 payload 的 enqueued_at（不依赖 arq 内部字段）
    enqueued_at = payload.get("enqueued_at")
    if enqueued_at:
        try:
            wait = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(enqueued_at)).total_seconds()
            AGENT_QUEUE_WAIT.observe(max(0.0, wait))
        except Exception:  # noqa: BLE001 时间戳异常不影响主流程
            pass

    pool = ctx.get("pg_pool")
    tenant = payload.get("tenant_id", "default")
    job_id = ctx.get("job_id")
    if pool and job_id:
        from tasks.store import get_task, create_task
        await create_task(pool, job_id, payload["channel"], {**payload, "tenant_id": tenant})
        cached = await get_task(pool, job_id, tenant)
        if cached is None:
            raise ValueError("Job tenant mismatch")
        if cached["status"] == "done":
            return cached["result"]
        if cached["attempts"] >= get_settings().task_max_tries:
            from tasks.store import update_task
            await update_task(pool, job_id, status="error", error="Execution attempts exhausted")
            raise RuntimeError("Execution attempts exhausted")
        async with pool.connection() as conn:
            await conn.execute("UPDATE async_tasks SET status='running',attempts=attempts+1,updated_at=now() WHERE task_id=%s", (job_id,))
    start = time.perf_counter()
    inbound = InboundMessage(
        channel=payload["channel"], user_id=payload["user_id"],
        text=payload["text"], conversation_id=payload["conversation_id"],
    )
    try:
        reply = await handle_message(
            inbound, ctx["checkpointer"], ctx["store"],
            tenant_id=payload.get("tenant_id", "default"),
            team=payload.get("team"),
            message_id=ctx.get("job_id"), pool=pool,
        )
        result = {"reply": reply, "conversation_id": inbound.conversation_id}
        if pool and job_id:
            from tasks.delivery import finish_task
            return await finish_task(pool, job_id, tenant, result, payload.get("reply_to"))
        if payload.get("reply_to"):
            raise RuntimeError("Durable delivery requires a database and job identity")
        return result
    except (WorkspaceBusy, TimeoutError, ConnectionError, httpx.TransportError,
            APIConnectionError, APITimeoutError, RateLimitError, InternalServerError):
        settings = get_settings()
        attempt = ctx.get("job_try", 1)
        if pool and job_id:
            from tasks.store import update_task
            await update_task(pool, job_id, status="queued" if attempt < settings.task_max_tries else "error", error="Transient execution failure")
        if attempt < settings.task_max_tries:
            raise Retry(defer=min(settings.task_retry_delay * (2 ** (attempt - 1)), 60)) from None
        raise
    except Exception as error:
        if pool and job_id:
            from tasks.store import update_task
            await update_task(pool, job_id, status="error", error=type(error).__name__)
        raise
    finally:
        # 端到端执行耗时（用户真正等的时间）
        AGENT_TASK_DURATION.observe(time.perf_counter() - start)


async def on_startup(ctx: dict) -> None:
    """Initialize persistence and delivery recovery before accepting jobs."""
    pool, checkpointer, store = await open_persistence()
    from infra.durable import init_durable_tables
    await init_durable_tables(pool)
    ctx["pg_pool"], ctx["checkpointer"], ctx["store"] = pool, checkpointer, store
    # worker 进程自己起一个 /metrics 端口（默认全局 registry；worker 单进程，简单够用）
    start_http_server(8002)
    logger.info("arq worker 启动：持久化就绪，指标端口 :8002")


async def on_shutdown(ctx: dict) -> None:
    """Release the worker-owned database pool."""
    if ctx.get("pg_pool"):
        await ctx["pg_pool"].close()

async def update_queue_length(ctx: dict) -> None:
    """周期性把队列长度写进 Gauge。"""
    try:
        jobs = await ctx["redis"].queued_jobs()      # arq 取待处理任务
        AGENT_QUEUE_LENGTH.set(len(jobs))
        if ctx.get("pg_pool"):
            from obs.metrics import DELIVERY_RECORDS
            async with ctx["pg_pool"].connection() as conn:
                cursor = await conn.execute("SELECT status,count(*) AS count FROM ibex_outbox GROUP BY status")
                counts = {row["status"]: row["count"] for row in await cursor.fetchall()}
            for status in ("pending", "sending", "sent", "dead"):
                DELIVERY_RECORDS.labels(status).set(counts.get(status, 0))
    except Exception as e:  # noqa: BLE001
        logger.warning("采集队列长度失败：{}", e)

class WorkerSettings:
    functions = [process_issue]
    on_startup = on_startup
    on_shutdown = on_shutdown
    from arq.connections import RedisSettings
    from infra.settings import get_settings
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = get_settings().task_max_tries
    job_timeout = get_settings().task_timeout
    from arq import cron
    from tasks.delivery import deliver_pending
    from tasks.queue import recover_jobs
    cron_jobs = [cron(update_queue_length, second=set(range(0, 60, 5))),
                 cron(deliver_pending, second=set(range(0, 60, 5))),
                 cron(recover_jobs, second={0, 15, 30, 45})]
