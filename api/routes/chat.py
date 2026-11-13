"""Real token streaming with deterministic resource cleanup."""
import json
import anyio
from infra.logging import get_logger
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from api.schemas import TaskRequest
from agent.main import build_agent_for_service
from channels.base import InboundMessage
from channels.session import thread_id_for
from sandbox.lifecycle import release_backend
from agent.execution import execution_session
from gateway.auth import tenant_for

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(TaskRequest):
    """The same conversation identity as the generic web channel."""
    source: str = "web"


@router.post("/stream")
async def stream_chat(body: ChatRequest, request: Request) -> StreamingResponse:
    """Stream model deltas, then release the sandbox on completion or disconnect."""
    tenant = tenant_for(request)
    inbound = InboundMessage(channel="webhook", user_id=body.source, text=body.text,
                             conversation_id=body.conversation_id or "default")
    thread = thread_id_for(inbound, tenant)
    async def events():
        try:
            async with execution_session(tenant, thread, request.app.state.checkpointer,
                    request.app.state.store, getattr(request.app.state, "pg_pool", None),
                    body.source, "webhook", body.team, factory=build_agent_for_service,
                    cleanup=release_backend) as (agent, _):
                config = {"configurable": {"thread_id": thread}}
                async for message, metadata in agent.astream(
                    {"messages": [{"role": "user", "content": body.text}]}, config, stream_mode="messages"):
                    if await request.is_disconnected():
                        return
                    if metadata.get("langgraph_node") != "model":
                        continue
                    content = message.content
                    text = content if isinstance(content, str) else "".join(
                        block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
                    if text:
                        yield "data: " + json.dumps({"text": text}, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
        except Exception:
            get_logger().exception("Streaming task failed")
            yield "data: " + json.dumps({"error": "Task failed; retry after any running request completes."}) + "\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
