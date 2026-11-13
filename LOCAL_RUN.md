# Ibex 本地实跑记录

> 后续已补齐鉴权、租户权限、持久工作区和投递恢复，并启动新版部署。最新状态见 [COMPLETION_REPORT.md](COMPLETION_REPORT.md)，本文保留为历史记录。

> 这是第一轮记录。后续全面验收、53 项回归及真实飞书测试结果见 [FULL_VERIFICATION.md](FULL_VERIFICATION.md)。当前进程号以 `logs/local-services.json` 为准；下文“未测试”描述只代表第一轮当时的范围。

日期：2026-11-13。结论：**修复若干运行阻塞后，真实模型请求、异步队列和一次沙箱编码任务已跑通；原始仓库并非直接启动即可完成这些流程。**

本地目录：`C:\Users\xiaohe\Documents\ChatGPT\实习\Ibex-Agent`。
来源：[21xiaohe/Ibex-Agent](https://github.com/21xiaohe/Ibex-Agent)，审阅快照提交 `174daf23d3ea63e06a4c7f24583aeacc5c059f75`。这是本地运行副本，修改尚未提交或推送到 GitHub。

## 已实际验证

| 项目 | 结果与边界 |
| --- | --- |
| Python 环境 | Python 3.13，`uv sync --frozen --python 3.13` 安装项目锁定依赖 |
| 基础设施 | 独立 PostgreSQL/pgvector 与 Redis 容器运行；使用持久化卷 |
| API | `/healthz`、`/readyz`、`/docs`、`/openapi.json`、`/metrics` 均返回 200 |
| 真实模型 | 复用旧项目授权的模型配置，qwen-max；通用 Webhook 实际返回 `IBEX_LOCAL_OK` |
| 队列 | `POST /jobs` 返回 job_id，独立 arq Worker 完成任务，查询得到 `IBEX_QUEUE_OK` |
| 会话持久化 | 同一会话续问成功；重启 API/Worker 后仍能复述此前标记，旧队列结果仍可查询 |
| 本地回归 | `tests` 与 `tests_service` 共 **13 passed** |
| 文件传输 | 加固容器内 256 字节二进制上传、下载完全一致 |
| 编码任务 | 第二次执行生成 Decimal 折扣函数与测试；原有 1 项、模型生成 4 项、独立验收 9 项，共 **14 passed** |
| 容器回收 | 本次任务容器均已销毁，结束检查时没有残留的任务沙箱 |

这些结果验证的是本次本地流程，不能据此声称生产可用、任务成功率高、自动重试可靠或完整评测已通过。

## 运行中发现并修复的问题

1. **飞书 Webhook 客户端用错。** 入队需要 arq 的 `enqueue_job()`，原代码传入普通 Redis 客户端。现改为 `app.state.arq_redis`；入队失败时释放去重标记，以便重投。用本地模拟事件验证，未连接真实飞书或向任何人发消息。
2. **通用 Webhook 缺少导入。** 补上 `InboundMessage` 和 `handle_message`。
3. **Windows 事件循环冲突。** psycopg 需要 Selector 循环，但该循环不支持 asyncio 子进程。增加本地启动入口，并在 Windows 上用线程执行 Docker 子进程。
4. **Agent 权限与沙箱不兼容。** 当前 deepagents 不支持给可执行命令的 SandboxBackend 配置这套 reviewer 文件权限，导致 Agent 构建直接失败。服务构建路径移除该配置；容器断网、只读根文件系统、非 root 用户及资源限制仍保留。
5. **服务路径注册了宿主机 Git 工具。** 这些工具会操作宿主机工作目录。服务 Agent 现只额外注册搜索工具；文件和命令通过沙箱后端执行。不再把宿主机 Git 提交、创建 PR 工具暴露给这个路径。
6. **沙箱初始化假成功。** 实测 `docker cp` 在当前只读容器上失败，原代码忽略返回码，并继续打印初始化成功。现通过容器内普通用户写入可写目录，检查每个文件的结果；构建或初始化失败时回收容器。
7. **产物导出失败。** 同一环境下原下载实现无法导出生成文件；改为容器内读取原始字节，经标准输出传输，增加错误处理和回归测试。
8. **沙箱镜像缺少样例测试依赖。** Dockerfile 补充 FastAPI、httpx，构建为 `ibex-sandbox:local`。
9. **原订单测试自身有错。** 补充 TestClient fixture；请求体与接口的数组契约对齐；期望金额从 37.25 更正为 `10.5×2 + 5.25×3 = 36.75`。

主要变更文件：`channels/webhook.py`、`agent/main.py`、`sandbox/docker_manager.py`、`sandbox/docker_sandbox.py`、`sandbox/Dockerfile`、`tests/test_orders.py`、`tests/conftest.py`、`tests_service/`、`scripts/run_local.py`、`scripts/smoke_local.py`。补丁另存于 `LOCAL_FIXES.patch`，不含 `.env`、密钥、虚拟环境或运行日志。

## 编码任务的真实表现

任务：实现 `apply_discount(amount: Decimal, rate: Decimal) -> Decimal`。要求负金额、范围外折扣率抛出 ValueError，使用 ROUND_HALF_UP 保留两位小数，并生成测试。

- **第一次没有完成任务。** 模型生成的测试有 1 项失败、7 项通过。失败来自额外编写的字符串输入测试预期异常不对；模型说会修改，但没有继续调用工具。随后导出步骤也触发了上述下载缺陷。失败工具记录保存在 `logs/coding-smoke/transcript.json`。
- **修复文件下载后，重新执行同一任务。** 模型生成代码和测试并完成执行；随后由验证脚本注入模型事先未见的 9 项用例，覆盖正常折扣、半分舍入、零金额、零/全额折扣、大额输入、负金额及越界折扣率。合计 14 项通过。
- 第二次的代码、模型测试、对话/工具记录和独立验收结果保存在 `logs/coding-smoke-2/`。原始输出中还有 pytest-asyncio 与 Starlette 的弃用提醒，不影响本次通过结果。

这里是一个任务的两次执行，不是标准评测集，也没有证明 Agent 能稳定自行修复失败。下一步最值得补的是“测试失败后必须继续修复或明确失败”的完成判定，以及多任务、固定判分的重复评测。

## 现在怎么使用

本次结束时 API 和 Worker 保持运行：

- API 文档：<http://127.0.0.1:18700/docs>
- 健康检查：<http://127.0.0.1:18700/readyz>
- PostgreSQL：本机 `15432`，容器 `ibex-local-postgres`。
- Redis：本机 `16379`，容器 `ibex-local-redis`。
- 本次 API PID 为 50304、Worker PID 为 62344；进程信息也写入 `logs/local-services.json`。重启后 PID 会变化。

打开 API 文档，展开 `POST /jobs`，点 Try it out，输入：

```json
{
  "text": "只回复：Ibex 本地运行成功",
  "user_id": "local-user",
  "conversation_id": "my-first-test"
}
```

执行后复制 job_id，再调用 `GET /jobs/{job_id}` 查询。该操作会实际调用已配置模型。

## 重新启动与复现

已有服务运行时不用重复启动。以后停止或重启电脑后，在 PowerShell 中：

```powershell
Set-Location 'C:\Users\xiaohe\Documents\ChatGPT\实习\Ibex-Agent'
docker start ibex-local-postgres ibex-local-redis
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe -m scripts.run_local web
```

另开一个 PowerShell，进入同一目录，运行 Worker：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe -m scripts.run_local worker
```

回归测试（不调用模型）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests tests_service --basetemp .pytest-local-run-manual
```

本机默认 pytest 临时目录存在访问权限问题，因此显式使用项目内的临时目录。`--basetemp` 是 pytest 专用目录，请勿指向放有个人文件的目录。

重新运行真实编码验收（会调用模型，会更新 `logs/coding-smoke-2/`）：

```powershell
.\.venv\Scripts\python.exe -m scripts.smoke_local
```

重新构建镜像：

```powershell
docker build -f sandbox/Dockerfile -t ibex-sandbox:local .
```

关闭时先核对进程仍对应本项目，再结束本项目的 API/Worker 进程；依赖容器可用 `docker stop ibex-local-postgres ibex-local-redis` 停止。保留命名卷即可保留本次数据库与队列数据。

## 配置和验证边界

- `.env` 仅在本机保存。按用户授权从旧项目复用模型相关配置，改用本次独立数据库、Redis 和沙箱镜像；未修改旧项目。
- 未复用飞书、Daytona、LangSmith 凭据；未执行真实飞书收发、外部 PR 发布或部署。
- 未测试五子代理团队、Daytona、只读 reviewer、MCP、故障切换、压测及原有完整 eval 流程。此次修复主要覆盖正式 Docker 服务路径。
- API 每个任务创建并销毁沙箱。PostgreSQL 会话历史能保留，但不等于生成的工作区文件能跨任务保留；普通 API 当前只返回回复文本。本次生成代码由专用验收脚本在销毁前导出。
- 单次冒烟通过不能支持“完整服务端实现”“稳定自动修复”“高任务成功率”等未经充分验证的简历表述。

原始证据：`logs/http-smoke.json`、`logs/queue-smoke.json`、`logs/restart-smoke.json`、`logs/coding-smoke/transcript.json`、`logs/coding-smoke-2/result.json`。运行日志位于 `logs/`，已在 Git 忽略规则中排除。
