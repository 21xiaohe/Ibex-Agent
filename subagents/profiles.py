"""subagents/profiles.py —— Ibex 的五个专职子代理定义

工程化要点（全部来自官方 subagents 最佳实践）：
- description 动作导向，主 Agent 据此决定委派；
- system_prompt 写全（子代理不继承主 Agent 的 prompt）；
- tools 最小化（只给该角色需要的，提升聚焦与安全）；
- model 按任务难度选（省成本/保质量）；
- Reviewer 用 response_format 输出结构化结论（可编程消费）。
"""
from langchain.chat_models import init_chat_model

from infra.settings import get_settings
from tools.registry import get_tools
from subagents.schemas import ReviewResult


def _model(name: str | None = None):
    """按需返回模型对象。默认用配置里的主模型；可传不同模型名做分级。
    这里用同一个 qwen 端点演示；真实项目可给不同角色配不同模型（见多模型路由）。"""
    s = get_settings()
    return init_chat_model(
        model=name or s.model_name,
        model_provider=s.model_provider,
        api_key=s.api_key.get_secret_value(),
        base_url=s.base_url,
        temperature=0,
    )


# subagents/profiles.py（节选：把每个子代理的 model 改成按角色路由）
from infra.llm_router import get_model_for_role
from tools.registry import get_tools
from subagents.schemas import ReviewResult


def build_subagents(workdir: str = "/home/daytona") -> list[dict]:
    # workdir 透传：和主规划一致，会动文件的子代理 prompt 里要带 workdir 绝对路径规则
    # （此处 system_prompt 用"……同前文"省略，实际内容含前文的 PATH_RULE）
    planner = {
        "name": "planner",
        "description": "把一个开发 Issue 拆解成清晰、有序、可执行的步骤。",
        "system_prompt": "你是资深技术规划者……（含 workdir 路径规则）",
        "tools": [],
        "model": get_model_for_role("planner"),     # strong 档（拆解要准）
    }
    researcher = {
        "name": "researcher",
        "description": "阅读相关代码、检索资料，为改动提供事实依据。",
        "system_prompt": "你是代码调研员……（含 workdir 路径规则）",
        "tools": get_tools("search"),
        "model": get_model_for_role("researcher"),   # cheap 档（读+总结，省成本）
    }
    coder = {
        "name": "coder",
        "description": "按既定计划编写或修改代码。",
        "system_prompt": "你是严谨的工程师……（含 workdir 路径规则）",
        "model": get_model_for_role("coder"),        # strong 档（写代码保质量）
    }
    tester = {
        "name": "tester",
        "description": "为改动写测试并在沙箱跑 pytest。",
        "system_prompt": "你是测试工程师……（含 workdir 路径规则）",
        "model": get_model_for_role("tester"),       # standard 档
    }
    reviewer = {
        "name": "reviewer",
        "description": "审查代码并做安全自查，给出结构化结论。",
        "system_prompt": "你是代码审查者……（含 workdir 路径规则）",
        "tools": get_tools(),
        "model": get_model_for_role("reviewer"),     # strong 档（审查要严）
        "response_format": ReviewResult,
    }
    return [planner, researcher, coder, tester, reviewer]