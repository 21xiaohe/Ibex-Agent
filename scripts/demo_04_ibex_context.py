"""scripts/demo_04_ibex_context.py —— 验证 Skills / Memory 生效

运行（项目根目录）：uv run python -m scripts.demo_04_ibex_context
"""
import asyncio

from agent.main import build_agent
from scripts._common import print_all


async def main():
    agent = build_agent(user_id="alice", channel="cli")

    # 这个任务会命中 fastapi-endpoint 规范，且涉及金额 → 应触发 AGENTS.md 的 Decimal 铁律
    task = (
        "在 app/ 下新增一个订单接口：POST /orders/quote，"
        "接收一组商品（单价、数量），返回订单总价。"
        "请遵循团队的接口规范，并按团队约定处理金额。"
    )

    # FilesystemBackend 下必须带 thread_id（checkpointer 需要）
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": task}]},
        config={"configurable": {"thread_id": "demo-04demo"}},
    )

    print_all(result)


if __name__ == "__main__":
    asyncio.run(main())