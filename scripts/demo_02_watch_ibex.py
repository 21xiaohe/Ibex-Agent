"""scripts/demo_02_watch_ibex.py —— 看 Ibex 一步步干活（异步流式 astream）

运行（项目根目录）：uv run python -m scripts.demo_02_watch_ibex
"""
import asyncio

from agent.main import build_agent

# 用多步任务，便于看到它先 write_todos 列计划、再 write_file 写文件
MULTI_STEP_TASK = (
    "为订单服务实现一个折扣计算模块，放在 discount.py：calc_subtotal、apply_coupon、"
    "calc_final_total 三个函数，带类型注解、docstring，并处理空列表与无效优惠券。"
)


async def main():
    agent = build_agent()

    # stream_mode="updates"：只打印每一步"新产生"的更新，最适合观察 Agent 决策过程
    async for chunk in agent.astream(
        {"messages": [{"role": "user", "content": MULTI_STEP_TASK}]},
        stream_mode="updates",
    ):
        for node_name, update in chunk.items():
            print(f"\n▶ 节点：{node_name}")
            msgs = update.get("messages") if isinstance(update, dict) else None
            if msgs:
                msgs[-1].pretty_print()   # 漂亮地打印这一步的消息（含工具调用）


if __name__ == "__main__":
    asyncio.run(main())