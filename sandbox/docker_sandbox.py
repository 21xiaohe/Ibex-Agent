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
    - upload_files    → docker cp 把宿主文件拷进容器
    - download_files  → docker cp 把容器文件拷出来读字节

同步 vs 异步
-----------
本类只实现同步方法（subprocess.run）。
BaseSandbox 基类会用 asyncio.to_thread 自动把它们包成异步版本（aexecute 等），
所以 agent 走 ainvoke 时不会阻塞事件循环，调用方无需关心线程细节。
"""
from __future__ import annotations

import os
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
        docker cp 只能从宿主机路径拷文件到容器，不能直接传字节流，
        所以需要先把字节写到宿主机临时文件，再 docker cp 进去。
        """
        results: list[FileUploadResponse] = []

        for path, content in files:
            try:
                # 第一步：确保容器内目标目录存在
                # 比如要写 /home/agent/app/utils/helper.py，
                # 需要先确保 /home/agent/app/utils/ 目录存在
                parent = os.path.dirname(path)
                if parent:
                    subprocess.run(
                        ["docker", "exec", self.container_id, "mkdir", "-p", parent],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,    # 不因 mkdir 失败而抛异常（目录可能已存在）
                    )

                # 第二步：把字节内容写到宿主机的临时文件
                # docker cp 需要一个宿主机上的文件路径作为源，没法直接传 bytes
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name

                # 第三步：用 docker cp 把临时文件拷进容器的目标路径
                cp = subprocess.run(
                    ["docker", "cp", tmp_path, f"{self.container_id}:{path}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                # 第四步：清理宿主机上的临时文件
                os.unlink(tmp_path)

                # 根据 docker cp 的退出码判断成功/失败
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
        与 upload_files 对称：docker cp 只能拷到宿主机路径，
        所以先 docker cp 到临时文件，再读出字节，最后清理临时文件。
        """
        results: list[FileDownloadResponse] = []

        for path in paths:
            tmp_path = None
            try:
                # 第一步：创建宿主机临时文件作为 docker cp 的目标
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp_path = tmp.name

                # 第二步：用 docker cp 把容器内文件拷到宿主机临时文件
                cp = subprocess.run(
                    ["docker", "cp", f"{self.container_id}:{path}", tmp_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                if cp.returncode == 0:
                    # 第三步：读出临时文件的全部字节内容
                    with open(tmp_path, "rb") as f:
                        data = f.read()
                    results.append(FileDownloadResponse(path=path, content=data))
                else:
                    # docker cp 失败，通常是容器内文件不存在
                    results.append(
                        FileDownloadResponse(path=path, content=None, error="file_not_found")
                    )

            except Exception as e:  # noqa: BLE001
                # 协议要求：部分成功，不抛异常
                results.append(FileDownloadResponse(path=path, content=None, error=str(e)))

            finally:
                # 无论成功还是失败，都要清理宿主机上的临时文件
                # finally 保证即使出异常也能执行清理
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        return results