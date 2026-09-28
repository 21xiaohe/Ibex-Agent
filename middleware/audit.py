# middleware/audit.py —— 工具审计埋点
import time
from collections.abc import Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest      # 注意点：来自 tool_node
from langgraph.types import Command

from infra.logging import get_logger
from obs.metrics import AGENT_TOOL_CALLS, AGENT_TOOL_DURATION

logger = get_logger()


class ToolAuditMiddleware(AgentMiddleware):
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], "Command | ToolMessage"],
    ) -> "Command | ToolMessage":
        tool_name = request.tool_call["name"]      # tool_call 是 dict，用 ['name']
        start = time.perf_counter()
        status = "ok"
        try:
            return await handler(request)
        except Exception:
            status = "error"
            raise
        finally:
            elapsed = time.perf_counter() - start
            AGENT_TOOL_CALLS.labels(tool_name, status).inc()        # 埋点
            AGENT_TOOL_DURATION.labels(tool_name).observe(elapsed)  # 埋点
            logger.info("🔧 工具 {} {}（{:.0f} ms）", tool_name, status, elapsed * 1000)