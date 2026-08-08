# tasks/worker.py（节选：on_startup 起指标端口 + 任务埋点）
import asyncio
import sys
import time
from datetime import datetime, timezone

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
    # 排队等待：用入队时塞进 payload 的 enqueued_at（不依赖 arq 内部字段）
    enqueued_at = payload.get("enqueued_at")
    if enqueued_at:
        try:
            wait = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(enqueued_at)).total_seconds()
            AGENT_QUEUE_WAIT.observe(max(0.0, wait))
        except Exception:  # noqa: BLE001 时间戳异常不影响主流程
            pass

    start = time.perf_counter()
    inbound = InboundMessage(
        channel=payload["channel"], user_id=payload["user_id"],
        text=payload["text"], conversation_id=payload["conversation_id"],
    )
    try:
        reply = await handle_message(
            inbound, ctx["checkpointer"], ctx["store"],
            tenant_id=payload.get("tenant_id", "default"),
        )
        return {"reply": reply, "conversation_id": inbound.conversation_id}
    finally:
        # 端到端执行耗时（用户真正等的时间）
        AGENT_TASK_DURATION.observe(time.perf_counter() - start)


async def on_startup(ctx: dict):
    pool, checkpointer, store = await open_persistence()
    ctx["pg_pool"], ctx["checkpointer"], ctx["store"] = pool, checkpointer, store
    # worker 进程自己起一个 /metrics 端口（默认全局 registry；worker 单进程，简单够用）
    start_http_server(8002)
    logger.info("arq worker 启动：持久化就绪，指标端口 :8002")


async def on_shutdown(ctx: dict):
    if ctx.get("pg_pool"):
        await ctx["pg_pool"].close()

async def update_queue_length(ctx: dict):
    """周期性把队列长度写进 Gauge。"""
    try:
        jobs = await ctx["redis"].queued_jobs()      # arq 取待处理任务
        AGENT_QUEUE_LENGTH.set(len(jobs))
    except Exception as e:  # noqa: BLE001
        logger.warning("采集队列长度失败：{}", e)

class WorkerSettings:
    functions = [process_issue]
    on_startup = on_startup
    on_shutdown = on_shutdown
    from arq.connections import RedisSettings
    from infra.settings import get_settings
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    from arq import cron
    cron_jobs = [cron(update_queue_length, second=set(range(0, 60, 5)))]