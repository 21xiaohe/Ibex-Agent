"""Shared persisted execution for all authenticated channels."""
from typing import Any
from channels.base import InboundMessage
from channels.session import thread_id_for
from agent.main import build_agent_for_service
from agent.execution import execution_session

async def handle_message(msg: InboundMessage, checkpointer: Any, store: Any,
                         tenant_id: str = "default", team: bool | None = None,
                         message_id: str | None = None, pool: Any = None) -> str:
    """Run in a tenant-owned workspace; partial edits survive retries and process restarts."""
    thread = thread_id_for(msg, tenant_id)
    async with execution_session(tenant_id, thread, checkpointer, store, pool,
                                 msg.user_id, msg.channel, team, factory=build_agent_for_service) as (agent, _):
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": msg.text, **({"id": message_id} if message_id else {})}]},
            config={"configurable": {"thread_id": thread}})
    return result["messages"][-1].content if result.get("messages") else ""
