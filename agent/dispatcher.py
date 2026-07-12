"""agent/dispatcher.py —— Ibex 团队任务编排入口

封装：构建团队 agent（含沙箱）→ 异步跑一个 Issue → 无论成败都清理沙箱。
这是"一个 Issue 进、结果出"的统一入口，后续会把它接进任务队列做后台长任务。
"""
from __future__ import annotations

from langchain_core.utils.uuid import uuid7

from agent.main import build_team_agent
from infra.logging import get_logger

logger = get_logger()


async def run_issue(
    issue: str,
    user_id: str = "anonymous",
    channel: str = "cli",
    fetch_paths: list[str] | None = None,
) -> dict:
    """跑一个 Issue：构建团队 + 沙箱，委派子代理协作，（可选）取回产物，最后清理沙箱。

    返回 agent 的最终状态 dict（含 messages / todos 等）。
    若传了 fetch_paths，会在关闭沙箱前把这些文件取回，放进返回值的 "artifacts" 键，
    便于验证"agent 是否真的在沙箱里改了文件"。
    """
    from sandbox.manager import download_artifacts

    thread_id = str(uuid7())   # 每个 Issue 一个 thread，对应一个独立沙箱
    agent, sandbox, client = build_team_agent(thread_id, user_id=user_id, channel=channel)

    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": issue}]},
            config={"configurable": {"thread_id": thread_id}},
        )
        logger.info("Issue 处理完成：thread_id={}", thread_id)

        # 在关闭沙箱前取回产物（验证 agent 真的改了文件 / 取回成果）
        if fetch_paths:
            # 需要 backend 来 download；从 agent 拿不到，这里直接用 sandbox 重建一个轻量 backend
            from langchain_daytona import DaytonaSandbox
            artifacts = download_artifacts(DaytonaSandbox(sandbox=sandbox), fetch_paths)
            result["artifacts"] = artifacts  # {沙箱路径: bytes}
        return result
    finally:
        # 关键：无论成功失败都回收沙箱，避免资源泄漏/持续计费（官方强调）
        try:
            sandbox.stop()
            logger.info("沙箱已回收：thread_id={}", thread_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("沙箱回收失败（thread_id={}）：{}", thread_id, e)