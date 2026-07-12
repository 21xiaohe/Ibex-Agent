# middleware/cost.py（节选：补 Prometheus 埋点，其余同前文）
import time
from collections.abc import Callable

from langchain.agents.middleware import (
    AgentMiddleware, AgentState, ModelRequest, ModelResponse, ExtendedModelResponse,
)
from langgraph.types import Command
from typing_extensions import NotRequired

from infra.logging import get_logger
from obs.metrics import record_llm_cost, AGENT_LLM_DURATION, AGENT_LLM_CALLS

logger = get_logger()


class CostState(AgentState):
    total_input_tokens: NotRequired[int]
    total_output_tokens: NotRequired[int]


class CostMeterMiddleware(AgentMiddleware):
    state_schema = CostState

    def __init__(self, tier: str = "strong") -> None:    # ← 新增 tier，用于给指标打标签
        super().__init__()
        self.tier = tier

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ExtendedModelResponse:
        start = time.perf_counter()
        try:
            response = await handler(request)
            AGENT_LLM_CALLS.labels(self.tier, "ok").inc()        # 埋点：调用成功
        except Exception:
            AGENT_LLM_CALLS.labels(self.tier, "error").inc()     # 埋点：调用失败
            raise                                                 # 失败也计数，再抛给 fallback 处理
        finally:
            AGENT_LLM_DURATION.labels(self.tier).observe(time.perf_counter() - start)  # 埋点：耗时

        in_tok, out_tok = self._extract_usage(response)
        record_llm_cost(self.tier, in_tok, out_tok)              # 埋点：token + 成本

        prev_in = request.state.get("total_input_tokens", 0)
        prev_out = request.state.get("total_output_tokens", 0)
        new_in, new_out = prev_in + in_tok, prev_out + out_tok
        logger.info("💰 模型调用[{}]：in={} out={} | 累计 in={} out={}",
                    self.tier, in_tok, out_tok, new_in, new_out)

        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={
                "total_input_tokens": new_in,
                "total_output_tokens": new_out,
            }),
        )

    @staticmethod
    def _extract_usage(response: ModelResponse) -> tuple[int, int]:
        msgs = getattr(response, "result", None) or getattr(response, "messages", None) or []
        for msg in reversed(msgs):
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        return 0, 0