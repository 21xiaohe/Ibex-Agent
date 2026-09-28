"""api/routes/issues.py —— 提交 Issue 的业务路由"""
from uuid import uuid4

from fastapi import APIRouter, Depends

from agent.main import build_agent_for_service
from api.schemas import IssueRequest, IssueResponse
from api.deps import get_checkpointer, get_store
from fastapi import APIRouter, Depends, Request   # ← Request 别漏（secure 端点用到）
from gateway.auth import require_api_key
from channels.base import InboundMessage
from channels.handler import handle_message

router = APIRouter(prefix="/issues", tags=["issues"])


@router.post("", response_model=IssueResponse)
async def submit_issue(
    req: IssueRequest,
    checkpointer=Depends(get_checkpointer),   # 注入 lifespan 建好的共享资源
    store=Depends(get_store),
) -> IssueResponse:
    # thread_id：客户端传了就用它（续聊/断点续跑），没传就新建
    thread_id = req.thread_id or uuid4().hex

    # 构建 agent（服务路径：按 sandbox_provider 绑定任务沙箱），传入共享持久化资源
    agent, sandbox = await build_agent_for_service(
        checkpointer=checkpointer,
        store=store,
        user_id=req.user_id,
        channel=req.channel,
    )

    try:
        # 异步调用，带 thread_id —— 同一 thread_id 下次请求会带着历史上下文继续
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": req.issue}]},
            config={"configurable": {"thread_id": thread_id}},
        )
    finally:
        if sandbox is not None:
            try:
                from sandbox.docker_manager import destroy_sandbox
                await destroy_sandbox(sandbox)
            except Exception:
                from infra.logging import get_logger
                get_logger().warning("任务沙箱清理失败（容器可能残留，需人工回收）", exc_info=True)

    reply = result["messages"][-1].content if result.get("messages") else ""
    return IssueResponse(thread_id=thread_id, reply=reply)


@router.post("/secure")
async def submit_issue_secure(
    req: IssueRequest,
    request: Request,
    tenant_id: str = Depends(require_api_key),   # ← 鉴权返回租户 id
):
    """需要 X-API-Key 的端点；租户身份贯穿到会话隔离。"""
    inbound = InboundMessage(
        channel=req.channel, user_id=req.user_id,
        text=req.issue, conversation_id=req.thread_id or req.user_id,
    )
    reply = await handle_message(
        inbound,
        request.app.state.checkpointer,
        request.app.state.store,
        tenant_id=tenant_id,                     # ← 一路传到 thread 派生，按租户隔离
    )
    return {"reply": reply, "tenant": tenant_id}