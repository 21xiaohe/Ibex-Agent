"""api/app.py —— FastAPI 入口 + lifespan（建池/持久化/挂资源）

工程化核心：
- 在 lifespan 建一个 AsyncConnectionPool，同时构造 AsyncPostgresSaver + AsyncPostgresStore；
- setup() 建表（首次）；把 checkpointer/store/redis 挂到 app.state 供全局复用；
- 关闭时统一释放——资源生命周期完全归服务所有，绝不在请求里临时建连。
"""
import os
import asyncio
from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from obs.metrics import PrometheusMiddleware
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from gateway.auth import authorize_request
from fastapi.responses import JSONResponse, FileResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from infra.db import build_pg_pool
from infra.redis import build_redis
from infra.logging import get_logger
from profiles import register_all_profiles
from api.schemas import HealthResponse
from api.routes.issues import router as issues_router
from channels.webhook import router as webhook_router
from fastapi.middleware.cors import CORSMiddleware
from api.routes.tasks_bg import router as tasks_bg_router   # ← 新增：import 路由
from tasks.store import init_task_table                     # ← 新增：import 建表函数
from api.routes.jobs import router as jobs_router    # ← 新增：import 路由
from tasks.queue import get_arq_redis                # ← 新增：import arq 池构造函数
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from gateway.limiter import limiter
from obs.metrics import RATELIMIT_REJECTED
from infra.settings import get_settings
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ① 注册 Harness Profile——在任何 agent 构建前
    register_all_profiles()

    # ② 建并打开 Postgres 连接池（一个池，喂 saver + store）
    pool = build_pg_pool()
    await pool.open()
    logger.info("Postgres 连接池已打开")

    from infra.durable import init_durable_tables
    await init_durable_tables(pool)

    # ③ 用同一个池构造异步 saver / store，并 setup（首次建表，幂等）
    checkpointer = AsyncPostgresSaver(pool)
    store = AsyncPostgresStore(pool)
    await checkpointer.setup()
    await store.setup()
    logger.info("AsyncPostgresSaver / AsyncPostgresStore 已 setup")

    # ④ Redis 池
    redis = build_redis()

    # ⑤ 挂到 app.state，供所有请求复用
    app.state.pg_pool = pool
    app.state.checkpointer = checkpointer
    app.state.store = store
    app.state.redis = redis
    arq_redis = await get_arq_redis()
    app.state.arq_redis = arq_redis
    try:
        yield                      # —— 服务运行中 ——
    finally:
        # ⑥ 关闭时统一释放
        await redis.aclose()
        await pool.close()
        await arq_redis.close()
        logger.info("连接池已释放")


app = FastAPI(title="Ibex API", lifespan=lifespan, dependencies=[Depends(authorize_request)])
app.include_router(issues_router)
app.include_router(webhook_router)
app.include_router(tasks_bg_router)
app.include_router(jobs_router)
from api.routes.chat import router as chat_router
app.include_router(chat_router)
from api.routes.deliveries import router as deliveries_router
app.include_router(deliveries_router)


@app.get("/", include_in_schema=False)
async def web_ui() -> FileResponse:
    """Serve the public sign-in page; every execution request still requires an API key."""
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent.parent / "web" / "index.html")

app.add_middleware(PrometheusMiddleware)          # 装 HTTP 采集中间件

app.state.limiter = limiter

# 自定义 429 处理器：先打指标，再走 SlowAPI 默认逻辑
async def rate_limit_handler_with_metrics(request, exc):
    # 从请求头映射到租户（和 gateway/limiter.py 的 _tenant_key 同一套逻辑）
    api_key = request.headers.get("X-API-Key")
    tenant = get_settings().api_keys.get(api_key) if api_key else None
    RATELIMIT_REJECTED.labels(tenant=tenant or "anonymous").inc()
    # 再交给 SlowAPI 默认处理器返回 429 响应
    return _rate_limit_exceeded_handler(request, exc)

app.add_exception_handler(RateLimitExceeded, rate_limit_handler_with_metrics)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 开发期放开；生产应收紧到你的前端域名
    allow_methods=["*"],
    allow_headers=["*"],
)

# 统一异常处理：不把内部堆栈暴露给客户端
@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception):
    logger.exception("未处理异常：{}", exc)
    return JSONResponse(status_code=500, content={"detail": "内部错误，请稍后重试"})


# 健康检查：liveness（纯探活）
@app.get("/healthz", response_model=HealthResponse)
async def healthz():
    return HealthResponse(status="ok")


# 健康检查：readiness（依赖就绪才 OK）
READINESS_TIMEOUT = 3.0


async def _probe_dependencies(pool, redis) -> None:
    async with pool.connection(timeout=2) as conn:
        await conn.execute("SELECT 1")
    await redis.ping()


def _consume_probe_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


@app.get("/readyz", response_model=HealthResponse)
async def readyz(request: Request):
    """Probe PostgreSQL and Redis with a bounded timeout before advertising readiness."""
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return JSONResponse(status_code=503, content={"status": "not_ready", "detail": "pool 未就绪"})
    probe = getattr(request.app.state, "readiness_probe", None)
    if probe is None or probe.done():
        probe = asyncio.create_task(_probe_dependencies(pool, request.app.state.redis))
        probe.add_done_callback(_consume_probe_result)
        request.app.state.readiness_probe = probe
    try:
        if probe.cancelling():
            raise TimeoutError
        done, _ = await asyncio.wait({probe}, timeout=READINESS_TIMEOUT)
        if not done:
            # psycopg cancellation may itself wait for the unavailable server.
            # Return promptly while one shared probe finishes its cleanup.
            probe.cancel()
            raise TimeoutError
        probe.result()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "detail": "dependency unavailable"})
    return HealthResponse(status="ready")

@app.get("/metrics")                              # 普通路由，避开 app.mount 的 307
async def metrics():
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        # 生产多进程（Gunicorn 多 worker）：聚合所有 worker
        from prometheus_client import CollectorRegistry, multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        data = generate_latest(registry)
    else:
        data = generate_latest()                  # 本地单进程：默认全局 registry
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


from sandbox.workspace import WorkspaceBusy
@app.exception_handler(WorkspaceBusy)
async def workspace_busy(request: Request, exc: WorkspaceBusy) -> JSONResponse:
    """A second writer must retry rather than overwrite a live conversation."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})
