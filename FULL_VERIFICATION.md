# Ibex 功能验收报告

> 后续已补齐鉴权、租户权限、持久工作区和投递恢复，并启动新版部署。最新状态见 [COMPLETION_REPORT.md](COMPLETION_REPORT.md)，本文保留为历史记录。

> 历史验收记录。随后针对已确认问题进行了修复，当前行为、复测证据和剩余限制见 [FIX_REPORT.md](FIX_REPORT.md)。

日期：2026-11-13。对象：本地运行副本及本次修复，原始快照提交 `174daf23d3ea63e06a4c7f24583aeacc5c059f75`。

**结论：核心流程可以运行，飞书真实收发也已通过；但不能说 README 宣称的全部功能都正常。** 有些是组件或独立示例，有些没有接入主流程，有些还是占位文件，另外还发现了明确缺陷。

本次完成 **53 项 pytest 回归测试、13 条评测用例各 3 次（共 39 次）、真实浏览器操作、Docker/Daytona/飞书/LangSmith 实测**。这些数字不是“53 个产品功能全部通过”，也不是任务成功率。所有修改只在本地，没有推送 GitHub。

## 1. 验证范围及结果

“真实通过”表示实际调用对应服务或运行代码；“受控通过”表示使用假模型或注入故障验证机制，不能替代真实模型质量验证。

| 功能 | 结果 | 实际证据与边界 |
| --- | --- | --- |
| 本地 API 启动、接口文档 | 真实通过 | `/healthz`、`/readyz`、`/docs`、`/metrics` 返回 200 |
| 同步 Issue 接口 | 真实通过 | `/issues` 返回真实模型生成的 `ISSUE_OK` |
| 通用 Webhook | 真实通过 | HTTP 请求进入 Agent，模型返回结果 |
| BackgroundTasks | 真实通过 | 提交后查询到 `done`、结果为 `BACKGROUND_OK`；错误状态另用故障注入验证 |
| arq 队列 | 真实通过 | 独立 Worker 执行、结果查询；飞书测试同样经过该队列 |
| 普通任务失败后自动重试 | **未通过** | 注入 RuntimeError 后只执行 1 次，任务失败；对照任务显式抛出 `Retry` 才执行第 2 次并成功 |
| Redis 原子幂等 | 真实通过 | 30 个并发请求，首次命中 1 个，重复命中 29 个 |
| 队列 job_id 去重 | 真实通过 | 相同 job_id 两次提交，Redis 队列只有 1 个条目 |
| 会话恢复 | 真实通过 | 第一轮已验证重启恢复；本轮验证持久化状态可正确还原 |
| API Key 鉴权 | 限定通过 | `/issues/secure` 无 Key 返回 401；合法 Key 成功。**其他入口并未统一要求鉴权** |
| 租户隔离 | 限定通过 | `/issues/secure` 两个租户使用相同会话名，数据库还原的消息历史互不混入。不能外推到未鉴权入口 |
| Redis 共享限流 | 真实通过 | 两个独立 Limiter 实例共享额度；同租户两个 Key 共同耗尽额度，第三次返回 429，其他租户仍返回 200 |
| LLM 并发闸 | 受控通过 | 8 个正常调用及 1 个故障调用，设置并发 2，峰值为 2；异常后名额释放。不是模型容量压测 |
| 请求上下文 | 受控通过 | request_id 每次不同，user/channel 正确保留 |
| 工具审计 | 修复后通过 | 异常和 `ToolMessage(status="error")` 能计入错误；原代码把后一种算成功 |
| Token/成本埋点 | 限定通过 | Token 累计正确；金额单价是代码里的示例配置，不能当作供应商真实账单 |
| 模型 fallback | 受控通过 | 主模型与第一备用模型注入失败，第二备用模型返回结果；本机三档实际配置均为 qwen-max，没有验证三个独立供应商 |
| AGENTS.md 记忆加载 | 真实通过 | 从持久化状态检查到非空的 AGENTS.md 内容 |
| PostgresStore | 真实通过 | 独立测试命名空间完成写入、读回、删除；不代表完整的个人长期记忆产品已实现 |
| 两个项目 Skills | **未实现** | `skills/fastapi-endpoint/SKILL.md` 和 `skills/unit-test/SKILL.md` 都是 0 字节；运行时被跳过 |
| Docker 基础隔离 | 真实通过 | UID 1000、network none、只读根文件系统、cap-drop ALL、512 MiB 内存和 256 PID 限制；根目录写入被拒绝。未进行容器逃逸安全评估 |
| 沙箱文件读写/导出 | 真实通过 | 二进制往返一致，生成代码导出成功 |
| 沙箱池 | 真实通过 | 上限 2、实测并发峰值 2，3 个任务使用 3 个新容器，全部回收；销毁失败时名额释放另有回归测试 |
| 独立物理只读沙箱 | 修复后通过 | 文件快照可读；覆盖、新建、删除、chmod 绕过均被拒绝 |
| 团队内 Reviewer 只读 | **未通过** | 在真实五角色图中强制发起写文件调用，Reviewer 成功写入临时文件，证明独立只读组件未接入团队 |
| 五角色委派及结构化审查 | 受控通过 | 原始角色定义和图编排可依次调用 planner/researcher/coder/tester/reviewer，返回 ReviewResult。使用确定性模型和临时目录，**没有证明真实模型团队能稳定协作完成开发任务** |
| 角色提示词 | **不完整** | 多个角色仍写着“……（含 workdir 路径规则）”，不是完整的角色说明 |
| Daytona | 限定通过 | 真正创建、按 thread_id 复用、上传下载、执行、删除。默认快照缺 pytest；在临时沙箱补装 pytest/FastAPI/httpx 后，样例测试通过 |
| 服务路径切换 Daytona | **未通过** | 设置 `sandbox_provider=daytona` 后，`build_agent_for_service` 仍调用 Docker 创建函数；团队入口另走 Daytona，两条路径没有统一 |
| gVisor / Kata | **缺少环境** | 本机只有 runc、io.containerd.runc.v2、nvidia runtime；没有 runsc/kata，未安装或冒充实测 |
| MCP | 真实组件通过 | 启动本地 stdio MCP 服务，发现并调用 echo 工具；另一个服务连接失败不影响它。正式服务构建函数没有加载 MCP，MCP 在独立构建入口 |
| HITL 人工审批 | 受控组件通过 | 真实图在工具执行前暂停，approve 才执行，reject 不执行；**普通 API/Worker 没有审批和恢复流程** |
| 本地 Git / pytest 工具 | 真实通过 | 在独立临时 Git 仓库执行 status、commit、diff、pytest。未修改用户仓库历史 |
| 创建 GitHub PR | **未实发** | 未创建真实 PR；没有确定发布目标、改动内容及发布授权。相关工具仍属于宿主机工具，不在正式沙箱服务中注册 |
| 搜索与网页抓取 | **未实现** | `web_search`、`fetch_url` 都返回“占位”文本，没有真实检索或抓取 |
| Web 页面 | 真实通过 | Edge 无头浏览器实际输入、点击发送、显示 `IBEX_BROWSER_OK`；页面无 JS 异常；用户文本 HTML 转义检查通过 |
| SSE 流式输出 | **未实现** | 页面先请求 `/chat/stream` 得到 404，再回退 `/webhook/generic` 成功。不是流式输出 |
| CLI | 修复后真实通过 | 增加 `--base-url`，连接 18700 并返回 `CLI_OK`；之前固定 localhost:8000 |
| 飞书凭据与实时收发 | 真实限定通过 | 凭据有效；用户授权后临时监听仅接收指定口令，入队、Worker 调模型、固定回复被飞书 API 接受；监听已关闭 |
| 飞书群列表权限 | **权限不足** | 飞书返回 99991672，缺少读取群信息权限；因此没有通过群列表获取测试会话 |
| 飞书 Webhook | 本地路由通过 | challenge、解析、入队、失败释放去重标记有本地验证；未配置公网回调或验证完整签名/加密接入 |
| LangSmith | 真实组件通过 | 在临时项目写入非敏感测试追踪，等待查询一致后读回结果，再删除测试项目；常驻 API 的 tracing 仍保持关闭 |
| 健康检查故障感知 | 修复后真实通过 | 分别暂停 Redis、PostgreSQL，约 3 秒返回 503；恢复容器后返回 200 |
| HTTP / 多进程指标 | 真实通过 | 应用镜像内 Gunicorn 多进程运行，50 次健康请求全部 200，聚合指标完整计入。不是 Agent 性能压测 |
| 沙箱活跃数 / fallback 指标 | **尚未接线** | 指标有定义，但代码没有对应 inc/dec 更新；不能据此展示可靠监控结果 |
| 应用 Docker 镜像 | 构建、启动通过 | 可启动 Gunicorn，健康接口正常 |
| 容器化 Agent 执行 | **未通过** | 镜像内没有 Docker CLI，真实 Agent 请求返回 500；Compose 也没接入 Docker daemon/远程沙箱控制服务。未擅自挂载宿主 Docker socket |
| Prometheus/Grafana 编排 | 配置检查，未全栈实跑 | 抓取配置存在；未启动完整监控编排，仓库没有预置 Dashboard，README 也将其列入 Roadmap |
| Locust 压测 | **未实现测试场景** | `loadtest/` 只有 `__init__.py`，没有可运行的 Locust 用户场景。安装了 locust 不等于完成压测 |
| 评测执行与记录 | 修复后真实通过 | 13 条用例各重复 3 次，原始回答、评分、代码产物和测试输出已保存 |
| CI 质量门禁 | 部分实现 | 修复了退化时退出码仍为 0 的问题，回归测试验证返回 1；未接入 GitHub Actions，历史基线也不适合直接作为可信门禁 |

## 2. 完整评测结果

这是仓库现有数据集的完整运行：1 条客观编码题、12 条开放题，每条重复 3 次。开放题由模型裁判评分，不是通过率。

| 维度 | 题数 | 平均分（0～1） |
| --- | ---: | ---: |
| 客观编码 exec | 1 | 1.000 |
| 领域问答 domain | 4 | 0.908 |
| 代码回答 code | 3 | 0.456 |
| 安全意识 safety | 3 | 0.989 |
| 通用问答 general | 2 | 0.967 |
| 按题平均 | 13 | 0.838 |

重要限制：

- 唯一客观编码题 3 次都通过，但只有一题，不能声称“开发任务成功率 100%”。
- code 维度波动明显。例如回文题得分 `[1.0, 0.2, 0.0]`；有时 Agent 只回复“已写入文件”，裁判看不到代码；HTTP 请求题还遇到断网沙箱无法访问外网。这同时反映任务完成方式、环境和评分口径的问题。
- 三档模型在本次配置中都是 qwen-max，回答模型与裁判也未实现独立模型验证。
- 旧基线来源、旧评分代码与本次修复后的口径不同；不能把 0.769 到 0.838 当作可靠的能力提升结论。
- 原评测器在另一个沙箱运行测试，并用输出文本猜是否通过；本次已改为同一 Agent 沙箱、检查真实退出码，保存原始记录。旧 exec=0 不能解释成模型完全不会编码。

原始证据：`logs/full-eval-results.json`、`logs/full-eval-run.log`。

## 3. 本轮新增修复

除第一轮 Webhook、Windows 启动和 Docker 文件传输修复外，本轮修复了：

1. `/jobs`、`/tasks` 缺少输入校验，空请求导致 500；现在返回 422。
2. CLI 固定端口，增加可配置 API 地址和 HTTP 错误检查。
3. HTTP 指标在路由匹配前取路径，造成任务 ID 标签膨胀；改在请求完成后取路由模板，404 使用固定标签。
4. 工具返回 error 状态仍被计为成功；现在正确计入错误。
5. 沙箱销毁抛异常后池名额不释放；现在通过 finally 释放。
6. 只读沙箱复制失败且仍可写；改为 root 所有的只读快照，禁止普通用户写入及 chmod 绕过。
7. Daytona 查询错误地把 SDK 迭代器当列表；改用 ListSandboxesQuery 和迭代器，修复复用。
8. Daytona 自动删除参数放错对象而被忽略；改放创建参数，增加 seed 失败检查与回收。
9. 评测工作区与 Agent 脱节、用字符串猜通过、缺少产物仍被静态检查判成功；现在绑定同一沙箱，检查退出码和文件存在。
10. 评测每次独立构建 Agent、独立 thread_id，保存每次回答和错误；退化时返回非零状态，便于未来接 CI。
11. Windows 下 arq 关闭使用不存在的 SIGUSR1；本地启动器改用兼容关闭方式。
12. 飞书同步 SDK 发送阻塞事件循环、失败仅打日志；改为线程执行并明确抛出发送失败，解析器明确过滤非文本消息。
13. readiness 只检查连接池对象是否存在；现在实际检查 PostgreSQL/Redis，并处理数据库取消操作拖延响应的问题。真实暂停测试已复测通过。

上述修复有对应 pytest 覆盖；最终结果 **53 passed, 1 warning**。警告来自飞书 SDK 使用弃用时间 API，不是测试失败。

## 4. 最值得继续修的内容

按“能否可靠演示项目”排序：

1. **统一运行入口和沙箱提供方。** 明确 API、团队、评测、MCP 各走哪条构建路径；解决 Daytona 配置被忽略及容器部署无法创建沙箱的问题。
2. **把 Reviewer 保护接进真实团队。** 独立只读组件已通过，不等于团队 Reviewer 只读；同时移除团队里的宿主机 Git/pytest 工具，补完整角色提示词。
3. **明确任务失败语义。** 针对可重试错误配置有限次数与退避，区分模型调用重试、整个任务重试和渠道发送重试。当前“可靠重试”表述过宽。
4. **补齐或删去占位功能声明。** 包括空 Skill、搜索、抓取、SSE、Locust 场景、未接线指标。不要为了验收表全绿而给空文件换个名字。
5. **扩大客观评测，并让评分看到真实产物。** 保留失败记录，分开统计功能正确性、规范遵守、任务完成率和模型裁判分数；重新建立有效基线。
6. **收紧服务边界。** `/issues/secure` 的验证不能替代所有入口的鉴权、租户隔离、飞书回调校验及审批流程。

## 5. 复现与产物

目录：`C:\Users\xiaohe\Documents\ChatGPT\实习\Ibex-Agent`。

```powershell
Set-Location 'C:\Users\xiaohe\Documents\ChatGPT\实习\Ibex-Agent'
$env:PYTHONIOENCODING = 'utf-8'

# 常驻服务：不要在已有进程运行时重复启动
.\.venv\Scripts\python.exe -m scripts.run_local web
# 另开终端
.\.venv\Scripts\python.exe -m scripts.run_local worker

# 回归测试（包含真实本地 Redis 限流和本地 MCP；不调用付费模型）
.\.venv\Scripts\python.exe -m pytest -q tests tests_service --basetemp .pytest-local-run-manual

# 完整评测，会调用模型；会更新原始结果文件
.\.venv\Scripts\python.exe -m eval.run_eval
```

`--basetemp` 只能指向专用测试目录，不要指向个人文件目录。

其他复现脚本：`scripts/verify_http_features.py`、`verify_sandbox_features.py`、`verify_queue_features.py`、`verify_team_graph.py`、`verify_web.py`、`verify_external.py`。浏览器验证额外使用了 playwright 和本机 Edge；playwright 只装进本地虚拟环境，没有修改项目锁文件。

`verify_external.py` 会使用本机旧配置，创建并清理临时 Daytona 沙箱和 LangSmith 项目；`verify_feishu_live.py` 会向收到口令的真实会话发送固定回复，不能作为无交互常规测试自动运行。本轮飞书发送已获用户明确授权，且只完成了一次。

| 证据文件 | 内容 |
| --- | --- |
| `logs/full-regression.txt` | 53 项回归测试结果 |
| `logs/http-features.json` | API、后台任务、鉴权、租户数据库隔离 |
| `logs/readiness-outage.json` | 依赖暂停约 3 秒返回 503，恢复后 200 |
| `logs/queue-features.json` | 原子幂等、队列去重、失败不自动重试及 Retry 对照 |
| `logs/sandbox-features-before.json` / `logs/sandbox-features.json` | 只读沙箱修复前后，以及真实沙箱池结果 |
| `logs/team-graph-verification.json` | 五角色图与 Reviewer 可写缺陷 |
| `logs/provider-routing.json` | 服务忽略 Daytona 配置的验证 |
| `logs/browser-verification.json` / `logs/browser-verification.png` | 浏览器真实请求、404 流式回退与页面截图 |
| `logs/feishu-live.json` | 飞书口令、队列、模型、固定回复的完整测试记录 |
| `logs/external-features-before.json` / `logs/external-features.json` | Daytona 和 LangSmith 的两轮结果及清理确认 |
| `logs/container-deployment.json` | 镜像健康检查 200，但 Agent 请求 500，Docker CLI 缺失 |
| `logs/multiprocess-metrics.json` | Gunicorn 多进程计数聚合，仅健康端点负载 |
| `logs/git-tools.json` / `logs/cli-verification.txt` | 临时 Git 仓库工具与真实 CLI 请求 |
| `logs/memory-verification.json` | AGENTS 记忆加载和 PostgresStore 读写 |

本轮结束时保留本地 API、Worker、PostgreSQL 和 Redis。当前进程号以 `logs/local-services.json` 为准，API 地址为 <http://127.0.0.1:18700/docs>。临时 Docker 应用容器、任务沙箱、Daytona 测试沙箱及 LangSmith 测试项目已清理；测试镜像保留以便复现。旧项目和 GitHub 均未改动。
