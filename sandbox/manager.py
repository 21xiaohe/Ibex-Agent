"""sandbox/manager.py —— 沙箱接入与生命周期管理（沙箱即工具模式）

工程化要点（来自官方 sandboxes 文档）：
- 按 thread_id 隔离：每个会话一个沙箱，get-or-create；
- TTL 自动回收：空闲沙箱由 provider 自动清理，避免一直计费；
- 密钥绝不进沙箱（见 agent/main.py 与本章 3.7 安全节）。

注意（高并发）：本章是"每会话一个沙箱"的基础形态。后半做高并发服务时，
要做成"沙箱池 + 并发隔离 + 超时回收"，并配合任务队列复用。
"""
from __future__ import annotations

from pathlib import Path

from infra.logging import get_logger

logger = get_logger()


def _collect_project_files(root: Path, dest_root: str, subdirs: list[str], extra_files: list[str]) -> list[tuple[str, bytes]]:
    """把宿主机项目里要 seed 进沙箱的文件，收集成 upload_files 需要的格式。

    返回 [(沙箱内绝对路径, 文件字节内容), ...]。
    官方要求：upload_files 的路径必须是绝对路径、内容必须是 bytes。
    dest_root 是沙箱的工作目录（如 /home/daytona）——把项目放到它下面，
    避免往 / 根目录写（沙箱默认用户无 root 权限，会 mkdir permission denied）。
    """
    items: list[tuple[str, bytes]] = []
    dest_root = dest_root.rstrip("/")   # 规范化，避免出现双斜杠

    # 1) 指定子目录（递归上传里面的文件，跳过缓存/隐藏目录）
    for sub in subdirs:
        base = root / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and not any(
                part.startswith(".") for part in p.relative_to(root).parts
            ):
                rel = p.relative_to(root).as_posix()       # 用正斜杠
                items.append((f"{dest_root}/{rel}", p.read_bytes()))

    # 2) 单独的根级文件（如 AGENTS.md）
    for fname in extra_files:
        f = root / fname
        if f.is_file():
            items.append((f"{dest_root}/{fname}", f.read_bytes()))

    return items


def seed_project_into_sandbox(backend, sandbox, root: Path) -> str:
    """把项目源码/技能/记忆 seed 进沙箱（agent 跑之前必须做）。返回沙箱工作目录。

    沙箱默认是空环境——不 seed 的话，agent 既看不到 app/ 也找不到 skills，
    coder 会"写不进文件"、skills 会报 path_not_found（这正是不 seed 的典型症状）。

    关键：seed 到沙箱的「工作目录」（Daytona 默认 /home/daytona），不要写死 /workspace——
    往 / 根目录建目录会因无 root 权限报 'mkdir /workspace: permission denied'。
    """
    # 取沙箱真实工作目录（Daytona：WORKDIR 或用户家目录，通常 /home/daytona）
    try:
        workdir = sandbox.get_workspace_root_dir()
    except Exception:
        workdir = "/home/daytona"   # 兜底默认
    workdir = (workdir or "/home/daytona").rstrip("/")

    files = _collect_project_files(
        root,
        dest_root=workdir,
        subdirs=["app", "tests", "skills"],   # 被测项目 + 测试 + 技能库
        extra_files=["AGENTS.md"],            # 仓库记忆
    )
    if not files:
        logger.warning("没有可 seed 的项目文件（app/ tests/ skills/ 都为空？）")
        return workdir
    # 官方 API：upload_files([(绝对路径, bytes), ...])；绝对路径在工作目录下，可写
    backend.upload_files(files)
    logger.info("已 seed {} 个文件进沙箱工作目录 {}", len(files), workdir)
    return workdir


def get_or_create_sandbox_backend(thread_id: str, project_root: Path | None = None):
    """按 thread_id 获取或创建一个沙箱后端（Daytona 示例），并在新建时 seed 项目。

    返回 (backend, sandbox, client, workdir)：
      - backend  传给 create_deep_agent(backend=...)
      - sandbox/client 用于生命周期管理（stop/delete）
      - workdir  沙箱工作目录（如 /home/daytona），用于拼 skills/memory 路径
    真实 provider 凭据从环境变量读（如 DAYTONA_API_KEY），不写死。

    实现说明（都用各版本都稳的 API，避免方法名/参数随版本变动踩坑）：
    - 查已有沙箱用 client.list(labels=...)（不用 find_one——它在某些版本不存在/会抛异常）；
    - 自动回收（TTL）是「客户端」配置 auto_delete_interval，放在 DaytonaConfig 里，
      不要塞进 create 的 params（塞错位置会引发奇怪的编码/请求错误）；
    - labels 只用纯 ASCII（如 thread_id），别放中文——labels 会进 HTTP header，
      非 latin-1 字符会报 UnicodeEncodeError。
    """
    from daytona import Daytona, DaytonaConfig
    from langchain_daytona import DaytonaSandbox

    root = project_root or Path(__file__).resolve().parent.parent  # ibex-agent/

    # 客户端：api_key/target 从环境变量读（见 3.3）；auto_delete_interval=60 表示
    # 持续停止 60 分钟后自动删除（TTL，单位分钟）。前提：入口已 load_dotenv()。
    client = Daytona(DaytonaConfig(auto_delete_interval=60))

    label = {"thread_id": thread_id}   # 纯 ASCII，安全

    # get-or-create：先按 label 查，有就复用，没有就新建
    is_new = False
    sandbox = None
    try:
        existing = client.list(labels=label)        # 返回匹配 label 的沙箱列表
        # 不同版本可能返回 list 或带 .sandboxes 的分页对象，两者都兼容
        items = getattr(existing, "sandboxes", existing) or []
        if items:
            sandbox = items[0]
            logger.info("复用已存在的沙箱：thread_id={}", thread_id)
    except Exception as e:  # noqa: BLE001 查不到/接口差异都走新建
        logger.info("查询已有沙箱失败（将新建）：{}", e)

    if sandbox is None:
        # 新建：最朴素的 create()，建好后打 label 便于下次复用
        sandbox = client.create()
        try:
            sandbox.set_labels(label)
        except Exception:  # noqa: BLE001 打 label 失败不影响使用
            pass
        is_new = True
        logger.info("新建沙箱：thread_id={}（TTL=60min）", thread_id)

    backend = DaytonaSandbox(sandbox=sandbox)

    if is_new:
        # 新建：seed 项目，并拿到工作目录
        workdir = seed_project_into_sandbox(backend, sandbox, root)
    else:
        # 复用：项目已在，只需取工作目录用于拼 skills 路径
        try:
            workdir = (sandbox.get_workspace_root_dir() or "/home/daytona").rstrip("/")
        except Exception:
            workdir = "/home/daytona"

    return backend, sandbox, client, workdir

def download_artifacts(backend, sandbox_paths: list[str]) -> dict[str, bytes]:
    """从沙箱取回指定文件（agent 跑完后用）。返回 {沙箱路径: 字节内容}。

    官方 download_files 返回结果含 .path / .content(bytes) / .error。
    """
    out: dict[str, bytes] = {}
    results = backend.download_files(sandbox_paths)
    for r in results:
        if getattr(r, "content", None) is not None:
            out[r.path] = r.content
        else:
            logger.warning("取回失败 {}：{}", getattr(r, "path", "?"), getattr(r, "error", "?"))
    return out