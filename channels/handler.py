"""channels/handler.py —— 渠道无关的消息处理核心

所有渠道把消息归一化成 InboundMessage 后，都调 handle_message。
这里不关心消息来自哪——这正是"渠道差异收敛在适配器、核心只认统一请求"的体现。
"""
from channels.base import InboundMessage
from channels.session import thread_id_for
from agent.main import build_agent
from infra.logging import get_logger

logger = get_logger()


# channels/handler.py（增强：handle_message 接收 tenant_id 并贯穿到 thread 派生与上下文）
async def handle_message(msg: InboundMessage, checkpointer, store, tenant_id: str = "default") -> str:
    thread_id = thread_id_for(msg, tenant_id)        # ← tenant 纳入 thread 派生（隔离）
    agent = build_agent(
        checkpointer=checkpointer, store=store,
        user_id=msg.user_id, channel=msg.channel,
        # 若 RequestContextMiddleware 支持 tenant，可一并传入（中间件增强）
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": msg.text}]},
        config={"configurable": {"thread_id": thread_id}},
    )
    return result["messages"][-1].content if result.get("messages") else ""
