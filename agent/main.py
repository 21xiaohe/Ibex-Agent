"""agent/main.py —— Ibex 主 Agent 构建入口

构建路径分三层：
- build_agent_for_service：正式服务路径（channels/worker/routes），按 settings.sandbox_provider 绑定 Docker 任务沙箱
- build_agent：本地退化路径（FilesystemBackend，无 Docker 环境/单测用）
- build_team_agent：五子代理团队 + 沙箱（复杂 Issue 的多角色协作）
"""
from pathlib import Path

from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langgraph.checkpoint.memory import MemorySaver

from infra.settings import get_settings
from infra.logging import get_logger
from middleware.audit import ToolAuditMiddleware
from middleware.cost import CostMeterMiddleware
from middleware.context import RequestContextMiddleware
from sandbox.manager import get_or_create_sandbox_backend
from subagents.profiles import build_subagents
from middleware.concurrency import LLMConcurrencyMiddleware
from subagents.reviewer import REVIEWER_PERMISSIONS
from infra.fallback import build_fallback_middleware

logger = get_logger()

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # ibex-agent/

IBEX_SYSTEM_PROMPT = """你是 Ibex，一个严谨的研发助手，服务于一个 Python / FastAPI 订单微服务团队。

工作准则：
- 动手前先用 write_todos 写出清晰的任务清单，并随进展更新它。
- 写代码遵循团队规范（见你已加载的 skills 与 AGENTS.md），不臆测，信息不足先读相关文件。
- 每次改动后，用一两句话说明"改了什么、为什么这么改"。
"""


def build_model():
    s = get_settings()
    return init_chat_model(
        model=s.model_name,
        model_provider=s.model_provider,
        api_key=s.api_key.get_secret_value(),
        base_url=s.base_url,
        temperature=0,
        max_retries=s.max_retries,
        timeout=s.timeout,
    )


from tools.registry import get_tools
from tools.registry import get_tools
from tools.mcp import MCPManager

async def build_agent_with_mcp(user_id: str = "anonymous", channel: str = "cli"):
    """带 MCP 工具的构建版本。MCP 工具需 await 获取，故本函数是 async。"""
    mcp_tools = await MCPManager.from_settings().get_tools()   # 容错地取回 MCP 工具
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    agent = create_deep_agent(
        model=build_model(),
        system_prompt=IBEX_SYSTEM_PROMPT,
        backend=backend,
        skills=[str(PROJECT_ROOT / "skills")],
        memory=[str(PROJECT_ROOT / "AGENTS.md")],
        tools=get_tools("git", "search", "test") + mcp_tools,   # 自定义工具 + MCP 工具
        middleware=[
            RequestContextMiddleware(user_id=user_id, channel=channel),
            ToolAuditMiddleware(),
            CostMeterMiddleware(),
        ],
        checkpointer=MemorySaver(),
    )
    logger.info("Ibex（含 MCP）构建完成：MCP 工具 {} 个", len(mcp_tools))
    return agent

def build_team_agent(thread_id: str, user_id: str = "anonymous", channel: str = "cli"):
    """五子代理团队 + 沙箱后端（已 seed 项目到沙箱工作目录）。

    - backend 用沙箱（自动提供文件工具 + execute），取代基础版的 FilesystemBackend；
    - 沙箱在新建时已被 seed 进 app/ tests/ skills/ AGENTS.md（见 sandbox/manager.py）；
    - skills/memory 指向沙箱工作目录下 seed 后的真实路径（workdir/skills 等）；
    - subagents 注入五个专职子代理；
    - 密钥（API key）只在主进程的 build_model() 里用，绝不传进沙箱。
    返回 (agent, sandbox, client) —— 后两者用于用完后清理。
    """
    # workdir 是沙箱工作目录（Daytona 默认 /home/daytona），项目就 seed 在它下面
    sandbox_backend, sandbox, client, workdir = get_or_create_sandbox_backend(thread_id)

    agent = create_deep_agent(
        model=build_model(),
        system_prompt=IBEX_SYSTEM_PROMPT + (
            f"\n\n【执行环境与路径规则——必须严格遵守】"
            f"\n你运行在一个沙箱里，项目代码已位于 `{workdir}/` 下（含 `{workdir}/app/`、`{workdir}/tests/`）。"
            f"\n⚠️ 所有文件操作（write_file/edit_file/read_file）和命令（execute）都【必须】使用以 `{workdir}/` 开头的【绝对路径】。"
            f"\n✅ 正确：写测试到 `{workdir}/tests/test_pricing.py`、改代码 `{workdir}/app/pricing.py`、"
            f"跑测试 `cd {workdir} && python -m pytest -q`。"
            f"\n❌ 错误（会因权限被拒绝，绝不要这样）：`/test_pricing.py`、`test_pricing.py`、`/app/pricing.py` 这类根路径或相对路径。"
            f"\n如果你不确定某文件在哪，先用 `ls {workdir}` 查看，再用绝对路径操作。"
            "\n\n你是团队负责人：对复杂 Issue，先用 task() 委派给 planner 规划，"
            "再依次委派 researcher/coder/tester/reviewer。你只做协调，不亲自写大量代码。"
            "委派时，把上面的【绝对路径规则】一并转达给子代理。"
        ),
        backend=sandbox_backend,                       # ← 沙箱后端：自动带 execute
        subagents=build_subagents(workdir),            # ← 五个专职子代理（把 workdir 传进去写进各自 prompt）
        skills=[f"{workdir}/skills"],                  # ← seed 后的真实路径（不再是空的 /skills/）
        memory=[f"{workdir}/AGENTS.md"],               # ← 同上
        middleware=[
            RequestContextMiddleware(user_id=user_id, channel=channel),
            ToolAuditMiddleware(),
            CostMeterMiddleware(),
        ],
        checkpointer=MemorySaver(),
    )
    logger.info("Ibex 团队版构建完成：5 子代理 + 沙箱后端（项目已 seed 到 {}）", workdir)
    return agent, sandbox, client


def build_agent(checkpointer=None, store=None, user_id="anonymous", channel="api", backend=None):
    """服务化版：checkpointer/store 由外部（lifespan）传入并复用。

    不传时退化为内存（仅供脚本/测试），传入则用 Postgres 持久化。
    backend=None 时用本地 FilesystemBackend（无 Docker 环境的退化路径）；
    显式传入沙箱 backend 则所有文件/命令操作都落在沙箱工作目录内。
    """
    if backend is None:
        backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    return create_deep_agent(
        model=build_model(),
        system_prompt=IBEX_SYSTEM_PROMPT,
        backend=backend,
        skills=[str(PROJECT_ROOT / "skills")],
        memory=[str(PROJECT_ROOT / "AGENTS.md")],
        tools=get_tools("git", "search"),
        middleware=[
            RequestContextMiddleware(user_id=user_id, channel=channel),
            ToolAuditMiddleware(),
            CostMeterMiddleware(),
            LLMConcurrencyMiddleware(),
            build_fallback_middleware(),
        ],
        checkpointer=checkpointer,      # ← 外部传入（lifespan 的 AsyncPostgresSaver）
        store=store,                    # ← 外部传入（lifespan 的 AsyncPostgresStore）
    )


SANDBOX_PATH_RULES = (
    "\n\n【执行环境与路径规则——必须严格遵守】"
    "\n你运行在一个隔离沙箱里，项目代码已位于 `{workdir}/` 下（含 `{workdir}/app/`、`{workdir}/tests/`）。"
    "\n⚠️ 所有文件操作（write_file/edit_file/read_file）和命令（execute）都【必须】使用以 `{workdir}/` 开头的【绝对路径】。"
    "\n✅ 正确：写测试到 `{workdir}/tests/test_pricing.py`、改代码 `{workdir}/app/pricing.py`、"
    "跑测试 `cd {workdir} && python -m pytest -q`。"
    "\n❌ 错误（会因权限被拒绝，绝不要这样）：`/test_pricing.py`、`test_pricing.py`、`/app/pricing.py` 这类根路径或相对路径。"
    "\n如果你不确定某文件在哪，先用 `ls {workdir}` 查看，再用绝对路径操作。"
)


async def build_agent_for_service(checkpointer=None, store=None, user_id="anonymous", channel="api"):
    """正式服务路径（channels/worker/routes）统一走这里：按 settings.sandbox_provider 绑定任务沙箱。

    docker → 每次任务新建加固容器并 seed 项目，Agent 的文件与命令操作全部落在
             沙箱工作目录内（密钥只在主进程 build_model() 使用，绝不进沙箱）；
             调用方负责在 finally 中 destroy_sandbox 清理（用完即弃）。
    none   → 本地 FilesystemBackend 退化路径（无 Docker 环境的演示/测试）。

    返回 (agent, sandbox_or_None)——sandbox 非 None 时调用方必须清理。
    """
    s = get_settings()
    if s.sandbox_provider == "none":
        return build_agent(checkpointer=checkpointer, store=store,
                           user_id=user_id, channel=channel), None

    from sandbox.docker_manager import create_one_sandbox, seed_project
    sb = await create_one_sandbox()
    await seed_project(sb)
    workdir = s.sandbox_workdir

    agent = create_deep_agent(
        model=build_model(),
        system_prompt=IBEX_SYSTEM_PROMPT + SANDBOX_PATH_RULES.format(workdir=workdir),
        backend=sb,                                # ← 沙箱后端：文件工具与 execute 全部进沙箱
        skills=[f"{workdir}/skills"],              # ← seed 后的真实路径
        memory=[f"{workdir}/AGENTS.md"],
        tools=get_tools("git", "search"),
        middleware=[
            RequestContextMiddleware(user_id=user_id, channel=channel),
            ToolAuditMiddleware(),
            CostMeterMiddleware(),
            LLMConcurrencyMiddleware(),
            build_fallback_middleware(),
        ],
        checkpointer=checkpointer,
        store=store,
    )
    logger.info("Ibex 服务版构建完成：Docker 沙箱已创建并 seed 项目到 {}", workdir)
    return agent, sb

async def build_sandbox_backend():
    """按 IBEX_SANDBOX_PROVIDER 选用沙箱后端，返回 (backend, workdir)。

    docker  → 自托管加固容器（本地可跑，需 Docker daemon）
    daytona → 外部托管沙箱
    两者都是 BaseSandbox，create_deep_agent(backend=...) 一视同仁。
    """
    s = get_settings()
    if s.sandbox_provider == "docker":
        from sandbox.docker_manager import create_one_sandbox, seed_project
        sb = await create_one_sandbox()
        await seed_project(sb)
        return sb, s.sandbox_workdir
    else:  # daytona —— 外部托管那套
        from sandbox.manager import get_or_create_sandbox_backend
        backend, sandbox, client, workdir = get_or_create_sandbox_backend("default")
        return backend, workdir