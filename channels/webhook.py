"""Authenticated generic and Feishu callback adapters."""
import json
from fastapi import APIRouter, Request, HTTPException
from pydantic import Field
from api.schemas import TaskRequest
from gateway.auth import tenant_for
from infra.settings import get_settings
from channels.base import InboundMessage
from channels.handler import handle_message
from channels.feishu_auth import verified_event, event_job_id
from tasks.queue import submit_job
router = APIRouter(prefix="/webhook", tags=["webhook"])

@router.post("/feishu")
async def feishu_webhook(request: Request) -> dict:
    """Verify the provider before durable deduplication and queue submission."""
    body = await verified_event(request)
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}
    header = body.get("header", {})
    if header.get("event_type") != "im.message.receive_v1":
        return {"code": 0}
    message = body.get("event", {}).get("message", {})
    if message.get("message_type") != "text":
        return {"code": 0}
    try:
        text = json.loads(message.get("content", "{}")).get("text", "").strip()
    except (ValueError, AttributeError):
        raise HTTPException(422, "Invalid text event") from None
    identifier, chat_id = header.get("event_id"), message.get("chat_id")
    if not identifier or not chat_id or not text:
        raise HTTPException(422, "Event identity, conversation and text are required")
    user = body.get("event", {}).get("sender", {}).get("sender_id", {}).get("open_id", "unknown")
    payload = {"channel": "feishu", "user_id": user, "text": text, "conversation_id": chat_id,
               "tenant_id": get_settings().feishu_tenant_id, "reply_to": {"type": "feishu", "chat_id": chat_id}}
    await submit_job(request.app.state.pg_pool, request.app.state.arq_redis, payload, job_id=event_job_id(identifier))
    return {"code": 0}

class GenericRequest(TaskRequest):
    """Validated generic channel message; tenant identity always comes from authentication."""
    source: str = Field(default="external", min_length=1, max_length=200)

@router.post("/generic")
async def generic_webhook(body: GenericRequest, request: Request) -> dict:
    """Run a message using the authenticated tenant and its persisted workspace."""
    inbound = InboundMessage(channel="webhook", user_id=body.source, text=body.text,
                             conversation_id=body.conversation_id or "default")
    reply = await handle_message(inbound, request.app.state.checkpointer, request.app.state.store,
                                 tenant_id=tenant_for(request), team=body.team, pool=request.app.state.pg_pool)
    return {"code": 0, "reply": reply}
