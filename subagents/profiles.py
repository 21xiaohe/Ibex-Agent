"""subagents/profiles.py —— Ibex 的五个专职子代理定义

工程化要点（全部来自官方 subagents 最佳实践）：
- description 动作导向，主 Agent 据此决定委派；
- system_prompt 写全（子代理不继承主 Agent 的 prompt）；
- tools 最小化（只给该角色需要的，提升聚焦与安全）；
- model 按角色分档（planner/coder/reviewer 用 strong，tester 用 standard，researcher 用 cheap）；
- Reviewer 用 response_format 输出结构化结论（可编程消费），并在独立只读快照里审查。
"""
from typing import Any

from langchain.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from deepagents import create_deep_agent
from infra.llm_router import get_model_for_role
from infra.settings import get_settings
from tools.registry import get_tools
from subagents.reviewer import REVIEWER_PERMISSIONS
from subagents.schemas import ReviewResult

_PATH_RULE = (
    "项目代码位于 `{workdir}/` 下，所有文件读写与命令执行必须使用以 `{workdir}/` 开头的绝对路径。"
)


def _build_reviewer_runnable(workdir: str, source_sandbox: Any):
    """构造 Reviewer 的 runnable：优先在独立只读快照里审查，退化为 FilesystemBackend + 只读权限。

    只读快照靠文件系统权限（Docker 容器 chmod -R a-w）保证，不靠 prompt 自律；
    审查完用 finally 销毁快照（用完即弃）。
    """
    prompt = "你是独立代码审查者，只读审查当前改动，检查正确性、测试证据与规范，返回结构化结论。" + _PATH_RULE.format(workdir=workdir)

    async def _review(state: dict, config: dict) -> dict:
        if source_sandbox is not None:
            from sandbox.docker_manager import create_readonly_sandbox, destroy_sandbox
            snapshot = await create_readonly_sandbox(source_sandbox)
            try:
                reviewer = create_deep_agent(
                    model=get_model_for_role("reviewer"),
                    system_prompt=prompt,
                    backend=snapshot,
                    tools=[],
                    subagents=[],
                    memory=[f"{workdir.rstrip('/')}/AGENTS.md"],
                    response_format=ReviewResult,
                )
                result = await reviewer.ainvoke({"messages": state["messages"]}, config)
                verdict = result.get("structured_response")
                if verdict is not None:
                    result["messages"].append(AIMessage(content=verdict.model_dump_json()))
                return result
            finally:
                await destroy_sandbox(snapshot)

        # 退化路径：无 Docker 沙箱时，用 FilesystemBackend + 只读权限（禁一切写）
        reviewer = create_deep_agent(
            model=get_model_for_role("reviewer"),
            system_prompt=prompt,
            backend=None,
            tools=[],
            subagents=[],
            permissions=REVIEWER_PERMISSIONS,
            response_format=ReviewResult,
        )
        result = await reviewer.ainvoke({"messages": state["messages"]}, config)
        verdict = result.get("structured_response")
        if verdict is not None:
            result["messages"].append(AIMessage(content=verdict.model_dump_json()))
        return result

    return RunnableLambda(_review)


def build_subagents(workdir: str = "/home/daytona", source_sandbox: Any = None) -> list[dict]:
    """构建五个专职子代理。source_sandbox 为 DockerSandbox 时，reviewer 在只读快照里审查。"""
    planner = {
        "name": "planner",
        "description": "把一个开发 Issue 拆解成清晰、有序、可执行的步骤。",
        "system_prompt": "你是资深技术规划者。" + _PATH_RULE.format(workdir=workdir),
        "tools": [],
        "model": get_model_for_role("planner"),     # strong 档（拆解要准）
    }
    researcher = {
        "name": "researcher",
        "description": "阅读相关代码、检索资料，为改动提供事实依据。",
        "system_prompt": "你是代码调研员。" + _PATH_RULE.format(workdir=workdir),
        "tools": get_tools("search"),
        "model": get_model_for_role("researcher"),   # cheap 档（读+总结，省成本）
    }
    coder = {
        "name": "coder",
        "description": "按既定计划编写或修改代码。",
        "system_prompt": "你是严谨的工程师。" + _PATH_RULE.format(workdir=workdir),
        "model": get_model_for_role("coder"),        # strong 档（写代码保质量）
    }
    tester = {
        "name": "tester",
        "description": "为改动写测试并在沙箱跑 pytest。",
        "system_prompt": "你是测试工程师。" + _PATH_RULE.format(workdir=workdir),
        "model": get_model_for_role("tester"),       # standard 档
    }
    reviewer = {
        "name": "reviewer",
        "description": "在独立只读快照中审查代码，给出结构化结论。",
        "runnable": _build_reviewer_runnable(workdir, source_sandbox),
    }
    return [planner, researcher, coder, tester, reviewer]
