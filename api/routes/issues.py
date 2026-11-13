"""Tenant-scoped synchronous issues sharing the persisted conversation workspace."""
from uuid import uuid4
from fastapi import APIRouter, Request
from api.schemas import IssueRequest, IssueResponse
from gateway.auth import tenant_for
from channels.base import InboundMessage
from channels.handler import handle_message
router = APIRouter(prefix="/issues", tags=["issues"])

@router.post("", response_model=IssueResponse)
async def submit_issue(req: IssueRequest, request: Request) -> IssueResponse:
    """Return the public conversation identity; internal checkpoint IDs are tenant-derived."""
    conversation = req.thread_id or uuid4().hex
    inbound = InboundMessage(channel=req.channel, user_id=req.user_id, text=req.issue, conversation_id=conversation)
    reply = await handle_message(inbound, request.app.state.checkpointer, request.app.state.store,
                                 tenant_id=tenant_for(request), team=req.team, pool=request.app.state.pg_pool)
    return IssueResponse(thread_id=conversation, reply=reply)

@router.post("/secure")
async def submit_issue_secure(req: IssueRequest, request: Request) -> dict:
    """Retain the existing secure URL; both issue routes now require authentication."""
    result = await submit_issue(req, request)
    return {**result.model_dump(), "tenant": tenant_for(request)}
