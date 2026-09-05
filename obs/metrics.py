"""
obs/metrics.py —— Prometheus 可观测性指标定义与采集中间件

核心设计：采用 RED 指标体系覆盖 HTTP 层全链路可观测性
- Rate（请求速率）：通过 Counter 统计总请求数，计算 QPS
- Errors（错误率）：通过状态码标签区分成功/失败请求，计算错误占比
- Duration（耗时）：通过 Histogram 统计请求耗时分布，计算 P50/P95/P99 分位值

同时提供在飞请求数 Gauge 指标，用于实时感知服务瞬时并发压力。
适配 Starlette / FastAPI 框架，支持多进程部署场景。
"""

import time

# prometheus_client 官方客户端库，三类核心指标类型说明：
# Counter: 单调递增计数器，适合统计总量、速率，重启后重置
# Histogram: 直方图，按预设桶统计样本分布，可计算分位数，适合耗时、大小类数据
# Gauge: 可增可减的仪表盘，适合统计瞬时值、当前状态量
from prometheus_client import Counter, Histogram, Gauge
# Starlette 基础中间件基类，用于实现 ASGI 层的全局请求拦截
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


# ------------------------------
# HTTP 层 RED 指标定义
# ------------------------------

# 请求总数计数器
# 用途：计算单维度 QPS、按路径/方法/状态码的请求量分布、接口错误率
# 标签说明：
#   method: HTTP 请求方法（GET/POST/PUT/DELETE 等）
#   path: 归一化后的路由路径，避免标签基数爆炸
#   status: HTTP 响应状态码，用于区分成功与各类错误请求
REQUEST_COUNT = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
)

# 请求耗时直方图
# 用途：统计接口耗时分布，计算 P50/P90/P99 等百分位耗时
# buckets 分桶设置：覆盖 50ms ~ 120s 区间，适配常规 API 与长耗时接口
# 标签维度：method + path，按接口粒度聚合耗时统计
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# 在飞请求数仪表盘
# 用途：监控服务当前正在处理的请求数量，评估瞬时并发压力、排查请求堆积
# multiprocess_mode="livesum": 多进程部署时，对所有存活 worker 的值求和
# 适配 Gunicorn / Uvicorn 多 worker 部署场景，避免多进程指标冲突
IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "当前正在处理的请求数",
    multiprocess_mode="livesum",
)

# obs/metrics.py（在 HTTP 指标后追加）

# ===== 成本类（LLM 服务特有，企业最该盯的之一）=====
AGENT_LLM_TOKENS = Counter(
    "agent_llm_tokens_total", "LLM token 消耗总量",
    ["tier", "direction"],          # tier: strong/standard/cheap; direction: input/output
)
AGENT_LLM_COST = Counter(
    "agent_llm_cost_yuan_total", "LLM 调用成本（元）", ["tier"],
)

# ===== 延迟/错误类（Agent 内部，HTTP 层看不到）=====
AGENT_LLM_DURATION = Histogram(
    "agent_llm_call_duration_seconds", "单次模型调用耗时（秒）", ["tier"],
    buckets=(0.2, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0),
)
AGENT_LLM_CALLS = Counter(
    "agent_llm_calls_total", "模型调用次数（按结果）", ["tier", "status"],  # status: ok/error
)
AGENT_TOOL_CALLS = Counter(
    "agent_tool_calls_total", "工具调用次数", ["tool", "status"],
)
AGENT_TOOL_DURATION = Histogram(
    "agent_tool_duration_seconds", "工具调用耗时（秒）", ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0),
)

# ===== 弹性类 =====
AGENT_FALLBACK = Counter(
    "agent_fallback_total", "模型 fallback 触发次数", ["from_tier", "to_tier"],
)
RATELIMIT_REJECTED = Counter(
    "ratelimit_rejected_total", "被限流拒绝（429）次数", ["tenant"],
)
IDEMPOTENCY_HITS = Counter(
    "idempotency_hits_total", "幂等命中（重复事件被挡）次数",
)

# ===== 任务/队列/沙箱类（饱和度，主要在 worker 进程产生）=====
AGENT_TASK_DURATION = Histogram(
    "agent_task_duration_seconds", "一个 Issue 端到端执行耗时（秒）",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)
AGENT_QUEUE_WAIT = Histogram(
    "agent_queue_wait_seconds", "任务入队到开始处理的等待（秒）",
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120),
)
AGENT_QUEUE_LENGTH = Gauge("agent_queue_length", "当前队列中待处理任务数")
SANDBOX_CREATE_DURATION = Histogram(
    "sandbox_create_duration_seconds", "沙箱创建（冷启动）耗时（秒）",
    buckets=(0.5, 1, 2, 5, 10, 30, 60),
)
SANDBOX_ACTIVE = Gauge("sandbox_active", "当前活跃沙箱数")

# 单价表（元 / 1K token），按你的实际计费填——把 token 换算成钱
# 真实企业里这张表通常从配置中心/计费服务动态拉，避免改价改代码
TOKEN_PRICE_PER_1K = {
    "strong":   {"input": 0.0024, "output": 0.0096},   # 示例：qwen-max
    "standard": {"input": 0.0008, "output": 0.0020},   # 示例：qwen-plus
    "cheap":    {"input": 0.0003, "output": 0.0006},   # 示例：qwen-turbo
}



def record_llm_cost(tier: str, input_tokens: int, output_tokens: int) -> None:
    """记录 token 用量并换算成钱。供 CostMeterMiddleware 调用。"""
    AGENT_LLM_TOKENS.labels(tier, "input").inc(input_tokens)
    AGENT_LLM_TOKENS.labels(tier, "output").inc(output_tokens)
    price = TOKEN_PRICE_PER_1K.get(tier, TOKEN_PRICE_PER_1K["standard"])
    cost = input_tokens / 1000 * price["input"] + output_tokens / 1000 * price["output"]
    AGENT_LLM_COST.labels(tier).inc(cost)


def _normalize_path(request: Request) -> str:
    """
    路径归一化处理，防止 Prometheus 标签基数爆炸。

    背景：若直接使用原始 URL 路径（如 /users/1001、/users/1002）作为标签，
    带参数的动态路径会产生海量不同标签值，导致 Prometheus 内存暴涨、查询性能下降。

    实现逻辑：
    1. 从请求 scope 中获取匹配到的路由对象
    2. 若存在路由模板，则返回模板路径（如 /users/{user_id}）
    3. 若未匹配到路由（如 404 路径），则回退为原始请求路径

    Args:
        request: Starlette 请求对象

    Returns:
        归一化后的路由路径字符串
    """
    # 从 ASGI scope 上下文中获取匹配到的路由对象
    route = request.scope.get("route")
    # 若路由对象存在且包含 path 属性（路由模板），返回模板路径
    if route and getattr(route, "path", None):
        return route.path
    # 未匹配到路由时，回退使用原始请求路径
    return request.url.path


class PrometheusMiddleware(BaseHTTPMiddleware):
    """
    Prometheus 指标采集中间件。

    基于 Starlette 基础中间件实现，拦截所有进入的 HTTP 请求，
    在请求生命周期的起点和终点埋点，自动采集 RED 指标与在飞请求数。

    核心特性：
    - 异常安全：使用 try-finally 保证即使请求抛出异常，指标也能正确更新
    - 高精度计时：使用 time.perf_counter 实现亚微秒级耗时统计
    - 路径归一化：自动转换参数化路径，避免标签基数膨胀
    """

    async def dispatch(self, request: Request, call_next):
        """
        中间件核心调度逻辑，包裹整个请求处理流程。

        Args:
            request: 进入的 HTTP 请求对象
            call_next: 下一个中间件 / 路由处理函数的调用入口

        Returns:
            HTTP 响应对象
        """
        # 前置处理：提前获取归一化路径与请求方法，作为指标标签公共值
        path = _normalize_path(request)
        method = request.method

        # 请求开始：在飞请求计数 +1
        IN_FLIGHT.inc()

        # 记录请求开始时间点
        # 使用 perf_counter 保证高精度计时，不受系统时间回拨、时区调整影响
        start = time.perf_counter()

        # 默认状态码设为 500，若请求处理过程中抛出异常，按服务端错误统计
        status = 500

        try:
            # 调用后续中间件与业务路由，获取响应对象
            response = await call_next(request)
            # 请求正常处理完成，更新为实际响应状态码
            status = response.status_code
            return response
        finally:
            # 后置处理：无论请求成功 / 抛出异常，都会执行本块逻辑，保证指标不丢失
            # 计算请求总耗时（单位：秒）
            elapsed = time.perf_counter() - start

            # 请求结束：在飞请求计数 -1，避免异常场景下计数泄漏
            IN_FLIGHT.dec()

            # 累加请求总数，按「方法 + 路径 + 状态码」维度统计
            REQUEST_COUNT.labels(method, path, str(status)).inc()

            # 记录请求耗时到直方图，按「方法 + 路径」维度统计耗时分布
            REQUEST_DURATION.labels(method, path).observe(elapsed)