"""sandbox/docker_manager.py —— 加固容器的创建 / seed / 销毁

本模块负责容器的 **生命周期管理**（创建、初始化、销毁），
而容器内部的 **命令执行与文件传输** 则由 DockerSandbox 负责。
两者分工：manager 管"生死"，sandbox 管"使用"。

隔离级别与 runtime 参数
-----------------------
runtime 参数是隔离级别的旋钮：
  - runc    : 标准 Linux 容器（cgroup + namespace），加固参数已大幅收窄攻击面
  - runsc   : gVisor 用户态内核，系统调用被二次拦截，安全性更高
  - kata    : 每个容器跑在独立 microVM 里，接近虚拟机级别隔离
切换 runtime 只需改配置，DockerSandbox 的代码完全不用动。

异步设计
--------
本模块异步管理容器生命周期；Windows Selector 循环使用线程执行子进程。
这是应用自身的异步逻辑（区别于 DockerSandbox 的同步方法——
后者由 BaseSandbox 通过 asyncio.to_thread 自动包装成异步）。
"""
from __future__ import annotations
import time
from obs.metrics import SANDBOX_CREATE_DURATION, SANDBOX_ACTIVE
import asyncio
import uuid
import subprocess
import sys
from pathlib import Path

from infra.settings import get_settings          # 统一配置：镜像名、资源限制、runtime 等
from infra.logging import get_logger              # 结构化日志
from sandbox.docker_sandbox import DockerSandbox  # 容器操作封装（execute / upload / download）

logger = get_logger()
_active_ids: set[str] = set()


# ---------- 内部工具：异步执行外部命令 ----------

async def _run(cmd: list[str]) -> tuple[int, str]:
    """异步执行一条外部命令，返回 (退出码, 合并后的输出文本)。

    参数
    ----
    cmd : list[str]
        要执行的命令及参数列表，例如 ["docker", "run", "-d", ...]。

    返回
    ----
    tuple[int, str]
        - int : 进程退出码。若 returncode 为 None（极端情况），回退为 -1。
        - str : stdout + stderr 合并后的 UTF-8 文本，解码失败的字节用 U+FFFD 替换。

    设计说明
    --------
    使用 asyncio.create_subprocess_exec 而非 subprocess.run，
    以避免在事件循环中阻塞（容器操作可能持续数秒甚至更久）。
    stderr 合并到 stdout，简化调用方的错误处理——
    只需检查退出码 + 一段文本即可判断成败。
    """
    if sys.platform == "win32":
        # psycopg 要求 SelectorEventLoop；该循环不支持 asyncio 子进程。
        result = await asyncio.to_thread(
            subprocess.run, cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return result.returncode, result.stdout.decode("utf-8", errors="replace")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,       # 捕获标准输出
        stderr=asyncio.subprocess.STDOUT,     # stderr 重定向到 stdout，统一处理
    )
    # communicate() 等待进程结束并一次性读完输出，避免管道缓冲区满导致死锁
    # 返回的第二个值是 stderr，因为已合并到 stdout 所以为 None
    out, _ = await proc.communicate()

    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", errors="replace"),
    )


# ---------- 核心 API 1：创建加固沙箱容器 ----------

async def create_one_sandbox() -> DockerSandbox:
    """以加固参数启动一个常驻容器，返回可操作的 DockerSandbox 实例。

    容器设计哲学——"最小权限 + 用完即弃"：
      1. 断网、丢能力、禁提权、只读根文件系统 -> 最小化攻击面
      2. 唯一可写区域是 tmpfs（内存文件系统） -> 容器销毁后数据随之蒸发，不残留
      3. 资源硬限（内存 / CPU / PID 数）      -> 防止恶意代码耗尽宿主资源

    返回
    ----
    DockerSandbox
        封装好的沙箱对象，可直接调用 execute / upload_files / download_files。

    异常
    ----
    RuntimeError
        容器启动失败时抛出，附带 docker 的错误输出。
    """
    start = time.perf_counter()
    s = get_settings()

    # 生成唯一容器名
    # 前缀 "syc-sbx-" 便于运维批量识别和清理沙箱容器
    # uuid4 取前 12 位十六进制，碰撞概率极低又不会太长
    name = f"syc-sbx-{uuid.uuid4().hex[:12]}"

    # 容器内的工作目录，通常为 /home/agent
    workdir = s.sandbox_workdir

    # 构造 tmpfs 挂载选项
    # tmpfs 是纯内存文件系统，容器销毁后自动消失
    #   rw   : 可读写（根文件系统是只读的，只有 tmpfs 可写）
    #   exec : 允许执行（agent 需要跑脚本和编译产物）
    #   size : 限制最大 512MB，防止写爆宿主内存
    #   uid  : 属主设为 1000（容器内非 root 用户），确保有写权限
    tmpfs_opt = f"{workdir}:rw,exec,size=512m,uid=1000"

    # 启动容器——docker run 参数逐行说明
    code, out = await _run([
        "docker", "run",
        "-d",                                       # 后台运行（detached），不阻塞当前进程
        "--name", name,                             # 容器名，后续通过它操作容器
        "--label", "ibex.owner=" + s.sandbox_owner,

        # -- 隔离级别 --
        "--runtime", s.sandbox_runtime,             # runc / runsc(gVisor) / kata(microVM)
                                                    # 这是隔离级别的唯一旋钮

        # -- 网络隔离 --
        "--network", "none",                        # 彻底断网，无 eth0，只有 lo
                                                    # 防止容器内代码外联 C2 或泄露数据

        # -- 能力与提权控制 --
        "--cap-drop", "ALL",                        # 丢弃全部 Linux capabilities
                                                    # 默认容器有约 14 项能力，这里全部移除
        "--security-opt", "no-new-privileges",      # 禁止通过 setuid / setgid / execve 提权
                                                    # 即使二进制文件有 suid 位也无效

        # -- 文件系统 --
        "--read-only",                              # 根文件系统只读，容器内无法篡改系统文件
        "--tmpfs", tmpfs_opt,                       # 工作目录挂 tmpfs，这是唯一可写区域
        "--tmpfs", "/tmp:rw,exec,size=128m,uid=1000",
                                                    # /tmp 也给一块可写 tmpfs
                                                    # 部分工具（编译器、pip）需要往 /tmp 写临时文件

        # -- 资源硬限 --
        "--memory", s.sandbox_mem_limit,            # 内存上限（如 "1g"），OOM 时内核直接杀进程
        "--memory-swap", s.sandbox_mem_limit,       # swap 设成和 memory 一样，实际上禁用 swap
                                                    # 防止恶意代码通过 swap 绕过内存限制
        "--pids-limit", str(s.sandbox_pids_limit),  # 最大进程/线程数（如 256），防 fork 炸弹
        "--cpus", s.sandbox_cpus,                   # CPU 配额（如 "1.0" = 1 核），防 CPU 独占

        # -- 用户 --
        "--user", "1000:1000",                      # 以 uid=1000 / gid=1000 运行，非 root
                                                    # 即使容器逃逸，宿主上也只是普通用户权限

        # -- 工作目录与入口命令 --
        "-w", workdir,                              # 设置容器内默认工作目录
        s.sandbox_image,                            # 使用的镜像（预装 Python / Node 等工具链）
        "sleep", "infinity",                        # 入口命令：无限睡眠，保持容器常驻
                                                    # 后续通过 docker exec 发送实际命令
    ])

    # 检查启动是否成功
    if code != 0:
        raise RuntimeError(f"起沙箱容器失败：{out}")

    logger.info("加固容器已起：{}（runtime={}）", name, s.sandbox_runtime)

    SANDBOX_CREATE_DURATION.observe(time.perf_counter() - start)

    # 返回 DockerSandbox 实例，后续所有操作都通过它进行
    _active_ids.add(name)
    SANDBOX_ACTIVE.inc()
    return DockerSandbox(container_id=name, workdir=workdir)


# ---------- 核心 API 2：向容器 seed（注入）项目文件 ----------

async def seed_project(
    sandbox: DockerSandbox,
    root: Path | None = None,
    subdirs: tuple[str, ...] = ("app", "tests", "skills"),
    extra_files: tuple[str, ...] = ("AGENTS.md",),
) -> None:
    """把宿主机上的项目文件 seed（注入）进容器的工作目录。

    为什么需要 seed？
    新创建的容器工作目录（tmpfs）是空的。如果不 seed：
      - coder agent 无法读取已有代码，也写不出有上下文的新代码
      - skills 相关功能会报 path_not_found
      - tests 目录不存在，无法跑测试验证
    所以在容器创建后、agent 开始工作前，必须把项目骨架拷进去。

    参数
    ----
    sandbox : DockerSandbox
        目标沙箱实例（已创建好的容器）。
    root : Path | None
        宿主机上的项目根目录。默认 None 时自动推断为本文件往上两级
        （即 sandbox/docker_manager.py -> sandbox/ -> 项目根/）。
    subdirs : tuple[str, ...]
        需要拷贝的子目录列表。只拷存在的目录，不存在的静默跳过。
        默认拷 app（应用代码）、tests（测试）、skills（技能配置）。
    extra_files : tuple[str, ...]
        需要额外拷贝的单文件列表。默认拷 AGENTS.md（agent 的指令文档）。
    """
    # 若未指定项目根，则自动推断：本文件 -> sandbox/ -> 项目根/
    root = root or Path(__file__).resolve().parent.parent

    files: list[tuple[str, bytes]] = []
    for sub in subdirs:
        directory = root / sub
        if directory.is_dir():
            for path in directory.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    files.append((f"{sandbox.workdir}/{path.relative_to(root).as_posix()}", path.read_bytes()))
    for name in extra_files:
        path = root / name
        if path.is_file():
            files.append((f"{sandbox.workdir}/{name}", path.read_bytes()))
    results = await asyncio.to_thread(sandbox.upload_files, files)
    if len(results) != len(files) or any(item.error for item in results):
        raise RuntimeError("Sandbox project initialization failed")
    logger.info("Project initialized in {} ({} files)", sandbox.container_id, len(files))


# ---------- 核心 API 3：销毁沙箱容器 ----------

async def destroy_sandbox(sandbox: DockerSandbox) -> None:
    """销毁容器，释放全部资源。

    设计说明
    --------
    - docker rm -f : 强制删除容器，即使仍在运行也先 kill 再 rm。
      沙箱是"用完即弃"的，没有优雅停机的需求。
    - tmpfs 上的所有文件随容器一起消失，宿主机上不会残留任何执行产物。
      这既是安全特性（防泄漏），也简化了清理逻辑。
    - 幂等性：如果容器已经不存在（如被手动清理），docker rm -f 会静默成功，不报错。
    """
    code, output = await _run(["docker", "rm", "-f", sandbox.container_id])
    if code and "No such container" not in output:
        raise RuntimeError("Sandbox removal failed")
    if sandbox.container_id in _active_ids:
        _active_ids.remove(sandbox.container_id)
        SANDBOX_ACTIVE.dec()
    logger.info("容器已销毁：{}", sandbox.container_id)


# sandbox/docker_manager.py（追加：只读 reviewer 用的沙箱）
async def create_readonly_sandbox(source_sandbox: DockerSandbox) -> DockerSandbox:
    """Create a root-owned snapshot that the agent cannot write, rename or chmod."""
    s = get_settings()
    name = f"syc-ro-{uuid.uuid4().hex[:12]}"
    workdir = source_sandbox.workdir
    code, out = await _run([
        "docker", "run", "-d", "--name", name,
        "--label", "ibex.owner=" + s.sandbox_owner,
        "--runtime", s.sandbox_runtime, "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--read-only",
        "--tmpfs", f"{workdir}:rw,exec,size=512m,uid=0,gid=0,mode=755",
        "--tmpfs", "/tmp:rw,size=128m,uid=1000",
        "--memory", s.sandbox_mem_limit, "--memory-swap", s.sandbox_mem_limit,
        "--pids-limit", str(s.sandbox_pids_limit), "--cpus", s.sandbox_cpus,
        "--user", "1000:1000", "-w", workdir, s.sandbox_image, "sleep", "infinity",
    ])
    if code != 0:
        raise RuntimeError(f"Readonly sandbox creation failed: {out}")
    _active_ids.add(name)
    SANDBOX_ACTIVE.inc()
    readonly = DockerSandbox(container_id=name, workdir=workdir)
    exporter = """import io,pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1])
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            data=path.read_bytes()
            info=tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size=len(data)
            archive.addfile(info,io.BytesIO(data))
"""
    importer = """import os,pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1])
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:
    archive.extractall(root,filter='data')
for directory,dirs,files in os.walk(root,topdown=False):
    for name in files: os.chmod(pathlib.Path(directory)/name,0o444)
    os.chmod(directory,0o555)
"""
    try:
        exported = await asyncio.to_thread(
            subprocess.run, ["docker", "exec", source_sandbox.id, "python", "-c", exporter, workdir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60,
        )
        await asyncio.to_thread(
            subprocess.run, ["docker", "exec", "-i", "-u", "0", name, "python", "-c", importer, workdir],
            input=exported.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60,
        )
        return readonly
    except BaseException:
        await destroy_sandbox(readonly)
        raise
