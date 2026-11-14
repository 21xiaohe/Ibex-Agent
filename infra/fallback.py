"""infra/fallback.py —— 模型 fallback(主模型失败退备用)

主模型(strong 档)限流/报错时,自动退到备用模型,保证可用性。
接入点就是前文的 llm_router：从 model_tiers 取主/备模型对象。
导入路径(官方确认)：from langchain.agents.middleware import ModelFallbackMiddleware
"""
from langchain.agents.middleware import ModelFallbackMiddleware

from infra.llm_router import get_model
from obs.metrics import AGENT_FALLBACK
from typing import Any


class MeasuredFallback(ModelFallbackMiddleware):
    """Count each attempted fallback without changing the library error handling."""
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Record async fallback attempts, including failed alternatives."""
        attempt = 0
        async def measured(current: Any) -> Any:
            nonlocal attempt
            if attempt:
                AGENT_FALLBACK.labels("strong" if attempt == 1 else "standard", "standard" if attempt == 1 else "cheap").inc()
            attempt += 1
            return await handler(current)
        return await super().awrap_model_call(request, measured)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        """Record synchronous fallback attempts with the same definition."""
        attempt = 0
        def measured(current: Any) -> Any:
            nonlocal attempt
            if attempt:
                AGENT_FALLBACK.labels("strong" if attempt == 1 else "standard", "standard" if attempt == 1 else "cheap").inc()
            attempt += 1
            return handler(current)
        return super().wrap_model_call(request, measured)


def build_fallback_middleware() -> ModelFallbackMiddleware:
    """主用 strong,失败退 standard,再退 cheap——按 model_tiers 配。

    主模型仍由 create_deep_agent(model=get_model("strong")) 指定;
    这条中间件在主模型抛错时依次尝试下面的备用模型(不含主模型本身)。
    ModelFallbackMiddleware 接收的是备用模型,可传字符串或 BaseChatModel 实例。
    """
    return MeasuredFallback(
        get_model("standard"),   # 第一备用
        get_model("cheap"),      # 第二备用
    )