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
    - upload_files    → docker exec -i 把字节写入容器内的可写目录
    - download_files  → docker exec 从容器读取原始字节

同步 vs 异步
-----------
本类只实现同步方法（subprocess.run）。
BaseSandbox 基类会用 asyncio.to_thread 自动把它们包成异步版本（aexecute 等），
所以 agent 走 ainvoke 时不会阻塞事件循环，调用方无需关心线程细节。
"""
from __future__ import annotations

import subprocess

from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileUploadResponse,
    FileDownloadResponse,
)

from infra.logging import get_logger

logger = get_logger()


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
            命令超时秒数。超时后 subprocess.run 会抛 TimeoutExpired 异常，
            防止恶意代码（如死循环）无限占用资源。None 表示不限时。

        返回
        ----
        ExecuteResponse
            - output    : 命令输出文本（stdout + stderr 合并）
            - exit_code : 进程退出码，0 表示成功
            - truncated : 输出是否被截断
        """
        # docker exec -w 指定在容器内哪个目录下执行命令
        # sh -lc 把 command 当作一整条 shell 命令来跑，支持管道、重定向等语法
        proc = subprocess.run(
            ["docker", "exec", "-w", self.workdir, self.container_id, "sh", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # 把 stderr 合并到 stdout，简化输出处理
            timeout=timeout,
        )

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
        """Write bytes via stdin as the container user, including with a read-only root.

        Parent directories are created inside the container. Each input has a matching
        response; failed writes are reported instead of claiming successful transfer.
        """
        results: list[FileUploadResponse] = []
        writer = (
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(sys.stdin.buffer.read())"
        )
        for path, content in files:
            try:
                proc = subprocess.run(
                    ["docker", "exec", "-i", self.container_id, "python", "-c", writer, path],
                    input=content, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=60,
                )
                results.append(FileUploadResponse(
                    path=path, error=None if proc.returncode == 0 else "permission_denied",
                ))
            except Exception:
                results.append(FileUploadResponse(path=path, error="permission_denied"))
        return results

    # ---------- 必须实现 4：下载文件 ----------

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read binary file contents as the container user, with per-file errors."""
        results: list[FileDownloadResponse] = []
        reader = (
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "sys.exit(2) if not p.is_file() else sys.stdout.buffer.write(p.read_bytes())"
        )
        for path in paths:
            try:
                proc = subprocess.run(
                    ["docker", "exec", self.container_id, "python", "-c", reader, path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                )
                error = None if proc.returncode == 0 else (
                    "file_not_found" if proc.returncode == 2 else "permission_denied"
                )
                results.append(FileDownloadResponse(
                    path=path, content=proc.stdout if error is None else None, error=error,
                ))
            except Exception:
                results.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
        return results
