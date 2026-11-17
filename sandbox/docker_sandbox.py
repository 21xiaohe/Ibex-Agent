"""sandbox/docker_sandbox.py —— 自托管加固容器沙箱（实现 BaseSandbox 的四个成员）

依赖：本机装好 Docker。

导入路径（官方确认）：
    from deepagents.backends.sandbox import BaseSandbox
    from deepagents.backends.protocol import (
        ExecuteResponse, FileUploadResponse, FileDownloadResponse
    )

整体设计
--------
容器在创建期就以加固参数起好并常驻（sleep infinity），
本类的四个成员通过 docker 命令操作那个容器：
    - id              → 容器名，用于外部识别
    - execute         → docker exec 跑命令
    - upload_files    → docker exec cat 写文件（只读 rootfs 下 docker cp 不可用）
    - download_files  → docker exec cat 读文件（同上）

同步 vs 异步
-----------
本类只实现同步方法（subprocess.run）。
BaseSandbox 基类会用 asyncio.to_thread 自动把它们包成异步版本（aexecute 等），
所以 agent 走 ainvoke 时不会阻塞事件循环，调用方无需关心线程细节。
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile

from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileUploadResponse,
    FileDownloadResponse,
)

from infra.logging import get_logger

logger = get_logger()

# execute 的默认超时（秒）。容器本身有 pids-limit 兜底，但一条死循环命令若无超时
# 会永久占住一个 asyncio.to_thread 工作线程——沙箱池少一个坑位，进程逐渐"线程饿死"。
# 300s 覆盖正常的编译/测试需求（eval 里跑 pytest 实测 <10s），又能在合理时间内
# 收割失控命令。退出码 124 与 GNU timeout 工具的约定一致。
DEFAULT_EXECUTE_TIMEOUT = 300


class DockerSandbox(BaseSandbox):
    """在一个加固 Docker 容器里执行命令、传输文件。

    本类只负责"使用"容器（执行命令、上传下载文件），
    容器的"生死"（创建、seed、销毁）由 docker_manager.py 负责。
    """

    def __init__(self, container_id: str, workdir: str = "/home/agent"):
        """
        参数
        ----
        container_id : str
            容器名称或 ID，由 docker_manager.create_one_sandbox() 生成，
            格式如 "syc-sbx-a1b2c3d4e5f6"。
        workdir : str
            容器内的默认工作目录，execute 执行命令时会 cd 到这里。
            默认 /home/agent，与 docker_manager 中 tmpfs 挂载点一致。
        """
        self.container_id = container_id
        self.workdir = workdir

    # ---------- 必须实现 1：唯一标识 ----------

    @property
    def id(self) -> str:
        """返回容器名作为沙箱的唯一标识，供框架层做路由和日志关联。"""
        return self.container_id

    # ---------- 必须实现 2：执行命令（同步）----------

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """在容器里跑一条 shell 命令，返回 ExecuteResponse。

        参数
        ----
        command : str
            要执行的 shell 命令字符串，会传给容器内的 sh -lc 执行。
            sh -l 表示以 login shell 启动，会加载 /etc/profile 等环境变量；
            -c 表示后面跟的是要执行的命令字符串。
        timeout : int | None
            命令超时秒数。None 时取 DEFAULT_EXECUTE_TIMEOUT（300s）——不设默认的话，
            一条死循环会永久占住 to_thread 工作线程。超时后命令被杀，返回
            exit_code=124 的正常响应（不裸抛异常），agent 能看到超时信息并自行调整。

        返回
        ----
        ExecuteResponse
            - output    : 命令输出文本（stdout + stderr 合并）
            - exit_code : 进程退出码，0 表示成功；超时为 124
            - truncated : 输出是否被截断
        """
        if timeout is None:
            timeout = DEFAULT_EXECUTE_TIMEOUT

        # docker exec -w 指定在容器内哪个目录下执行命令
        # sh -lc 把 command 当作一整条 shell 命令来跑，支持管道、重定向等语法
        try:
            proc = subprocess.run(
                ["docker", "exec", "-w", self.workdir, self.container_id, "sh", "-lc", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # 把 stderr 合并到 stdout，简化输出处理
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            # 超时不裸抛 TimeoutExpired：协议消费方（agent 工具层）拿异常只会看到
            # 一条 traceback，而正常响应能让 agent 读到"超时"事实并决策下一步。
            # 把已产出的部分输出一并带回去，尽量不浪费已完成的计算。
            partial = e.stdout if isinstance(e.stdout, bytes) else (e.stdout or "").encode("utf-8", errors="replace")
            text = partial.decode("utf-8", errors="replace")
            text = f"{text}\n[ibex] command exceeded timeout={timeout}s, killed (exit 124)"[-8000:]
            logger.warning("沙箱命令超时被杀：{}（timeout={}s）", self.container_id, timeout)
            return ExecuteResponse(output=text, exit_code=124, truncated=len(partial) > 8000)

        # 解码输出文本，遇到无法解码的字节用 U+FFFD (�) 替换，不会抛异常
        text = proc.stdout.decode("utf-8", errors="replace")

        # 判断是否需要截断：如果输出超过 8000 字符，只保留最后 8000 字符
        # 保留尾部而非头部，因为尾部通常包含错误信息和最终结果，对 agent 更有用
        # 这样做也避免巨量输出灌爆 LLM 的上下文窗口
        truncated = len(text) > 8000

        return ExecuteResponse(
            output=text[-8000:],        # 截断：只保留最后 8000 字符
            exit_code=proc.returncode,
            truncated=truncated,
        )

    # ---------- 必须实现 3：上传文件 ----------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """把文件内容写进容器。write_file / edit_file 等高层操作底层都依赖这个方法。

        参数
        ----
        files : list[tuple[str, bytes]]
            每个元素是 (容器内绝对路径, 文件字节内容)。
            例如 [("/home/agent/app/main.py", b"print('hello')")]

        返回
        ----
        list[FileUploadResponse]
            与输入一一对应的结果列表。每个元素包含 path 和可选的 error 字段。
            支持部分成功：某个文件上传失败不影响其他文件，也不抛异常。

        实现思路
        --------
        **不能用 docker cp**：沙箱容器以 --read-only（只读 rootfs）+ tmpfs 工作目录
        启动，docker cp 走 daemon 归档接口，在只读 rootfs 容器上一律报
        "container rootfs is marked read-only" 失败（实测 2026-11-17）。
        改用 docker exec 起进程写 tmpfs：mkdir -p 建父目录 + `cat > path` 从 stdin
        收字节流——exec 的进程有 tmpfs 写权限，完全绕开 rootfs 限制。
        """
        results: list[FileUploadResponse] = []

        for path, content in files:
            try:
                # 第一步：确保容器内目标目录存在（exec 以容器默认用户 agent 运行，
                # /home/agent 是 uid=1000 的 tmpfs，agent 自己有权 mkdir）
                parent = os.path.dirname(path)
                if parent:
                    subprocess.run(
                        ["docker", "exec", self.container_id, "mkdir", "-p", parent],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,    # 不因 mkdir 失败而抛异常（目录可能已存在）
                    )

                # 第二步：docker exec -i 起 cat 进程，文件字节从 stdin 喂进去
                # shlex.quote 防路径里的特殊字符被 shell 解释
                cp = subprocess.run(
                    ["docker", "exec", "-i", self.container_id,
                     "sh", "-c", f"cat > {shlex.quote(path)}"],
                    input=content,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                # 第三步：按退出码判断成功/失败
                if cp.returncode == 0:
                    results.append(FileUploadResponse(path=path))
                else:
                    err = cp.stdout.decode("utf-8", errors="replace") or "upload_failed"
                    results.append(FileUploadResponse(path=path, error=err))

            except Exception as e:  # noqa: BLE001
                # 协议要求：部分成功，不抛异常
                # 即使某个文件上传出错，也记录 error 并继续处理下一个
                results.append(FileUploadResponse(path=path, error=str(e)))

        return results

    # ---------- 必须实现 4：下载文件 ----------

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """从容器读出指定路径的文件字节。

        参数
        ----
        paths : list[str]
            容器内的绝对路径列表，例如 ["/home/agent/app/main.py"]

        返回
        ----
        list[FileDownloadResponse]
            与输入一一对应的结果列表。每个元素包含 path、content（bytes 或 None）
            和可选的 error 字段。
            支持部分成功：某个文件下载失败不影响其他文件，也不抛异常。

        实现思路
        --------
        与 upload_files 对称：**不用 docker cp**（只读 rootfs 容器上 docker cp
        会被 daemon 拒绝，见 upload_files 的说明），改用 `docker exec cat` 直接
        捕获 stdout 拿到文件字节——读取不涉及写 rootfs，exec 通道畅通。
        """
        results: list[FileDownloadResponse] = []

        for path in paths:
            try:
                # docker exec 起 cat 读文件，字节直接从 stdout 拿
                cp = subprocess.run(
                    ["docker", "exec", self.container_id, "cat", path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                if cp.returncode == 0:
                    results.append(FileDownloadResponse(path=path, content=cp.stdout))
                else:
                    # exec cat 失败，通常是容器内文件不存在
                    err = cp.stderr.decode("utf-8", errors="replace") or "file_not_found"
                    results.append(
                        FileDownloadResponse(path=path, content=None, error=err)
                    )

            except Exception as e:  # noqa: BLE001
                # 协议要求：部分成功，不抛异常
                results.append(FileDownloadResponse(path=path, content=None, error=str(e)))

        return results