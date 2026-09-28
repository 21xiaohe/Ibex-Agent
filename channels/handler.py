"""channels/handler.py —— 渠道无关的消息处理核心

所有渠道把消息归一化成 InboundMessage 后，都调 handle_message。
这里不关心消息来自哪——这正是"渠道差异收敛在适配器、核心只认统一请求"的体现。

执行环境：走 build_agent_for_service，按 settings.sandbox_provider 决定是否
在 Docker 任务沙箱中执行；沙箱用完即弃，清理失败不阻断响应但记录日志。
"""
from channels.base import InboundMessage
from channels.session import thread_id_for
from agent.main import build_agent_for_service
from infra.logging import get_logger

logger = get_logger()


async def handle_message(msg: InboundMessage, checkpointer, store, tenant_id: str = "default") -> str:
    thread_id = thread_id_for(msg, tenant_id)        # ← tenant 纳入 thread 派生（隔离）
    agent, sandbox = await build_agent_for_service(
        checkpointer=checkpointer, store=store,
        user_id=msg.user_id, channel=msg.channel,
    )
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": msg.text}]},
            config={"configurable": {"thread_id": thread_id}},
        )
    finally:
        if sandbox is not None:
            try:
                from sandbox.docker_manager import destroy_sandbox
                await destroy_sandbox(sandbox)
            except Exception:
                logger.warning("任务沙箱清理失败（容器可能残留，需人工回收）", exc_info=True)
    return result["messages"][-1].content if result.get("messages") else ""
