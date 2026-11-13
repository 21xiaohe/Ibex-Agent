# Ibex 剩余功能补齐与部署验收

日期：2026-11-13。本文覆盖上一轮明确留下的鉴权、租户权限、工作区持久化、独立投递重试，以及配套的部署和恢复验证。它取代 `FIX_REPORT.md` 中对应的“尚未实现”状态。

## 结论

四项功能缺口已补齐，并通过实际数据库、队列、Docker 部署和浏览器验证。Windows、Linux 应用容器的完整回归均为 **99 passed**。已启动独立的完整部署，当前可以直接使用：

- 网页：<http://127.0.0.1:18704/>
- API 文档：<http://127.0.0.1:18704/docs>
- Prometheus：<http://127.0.0.1:18705/>
- Grafana：<http://127.0.0.1:18706/>，已配置数据源和 6 个面板。

网页需要填写 API Key。密钥保存在本地 `.env` 的 `IBEX_API_KEYS` 映射中：键是 API Key，值是所属租户。Grafana 用户名为 `admin`，密码是 `.env` 中的 `IBEX_GRAFANA_PASSWORD`。密钥不写入浏览器存储，也不写入报告或补丁。

请使用上述新版地址。原来的 18700 是此前保留的旧本地演示进程，不代表本次新部署的行为。新版使用单独的 Compose 项目 `ibex-completion`、数据库和 Redis 卷，不依赖旧 API/Worker。

## 已补齐的行为

| 项目 | 当前行为 | 验证 |
| --- | --- | --- |
| 全业务入口鉴权 | `/issues`、`/issues/secure`、`/jobs`、`/tasks`、通用 Webhook、SSE、结果查询、投递查询/重试均要求 API Key；不再内置可用的示例密钥 | 实际应用路由测试；新部署所有提交入口无密钥均返回 401 |
| 租户资源权限 | 身份来自服务端密钥映射，忽略请求正文中的租户冒充；任务结果和投递记录按租户查询，跨租户返回 404；会话 ID 使用结构化编码，避免分隔符碰撞 | 真实 PostgreSQL 及 HTTP 查询测试；另一租户无法读取同名会话文件 |
| 文件工作区持久化 | PostgreSQL 保存完整文件清单及字节内容；每次新沙箱恢复上一版本，删除操作也会恢复；同一租户会话使用数据库锁跨进程互斥 | 写文件后重启 Web，再真实调用 `read_file` 成功读回；并发锁、删除、二进制文件和超限保护均有测试 |
| 临时文件模式隔离 | `sandbox_provider=none` 使用每次独立的临时副本，不再把服务仓库作为各租户共用的可写目录 | 实际服务构建、MCP 调用和持久化测试覆盖 |
| 任务持久提交 | `/tasks` 与 `/jobs` 统一进入持久队列；先存数据库再入队；提交中断或队列发布失败由 Worker 扫描补发 | 注入 Redis 发布失败后，数据库任务仍存在，恢复后使用同一任务 ID 补发 |
| 执行恢复与结果缓存 | 任务有持久尝试次数；已完成任务返回缓存结果；超时留下的 arq 失败结果不会永久阻止未完成数据库任务恢复 | 回归覆盖重试上限、缓存结果、失败结果清理；真实 Worker 完成 `/tasks` 提交的任务 |
| 独立投递重试 | 模型结果和待投递消息在同一数据库事务提交；发送失败只重试投递，指数退避；耗尽次数保存为 `dead`；可查询并手动重投 | 真实 PostgreSQL 中模拟发送失败、耗尽、重投和发送进程中断，模型只执行一次 |
| 飞书入口保护 | Webhook 校验 token、应用身份；启用加密时使用 SDK 解密并校验签名与时间；事件 ID 参与持久去重；长连接也使用稳定事件 ID 入队 | 签名/过期请求、解析、入队客户端和发送 SDK 回归；本轮没有再次向真实飞书会话发消息 |
| 沙箱崩溃回收 | 容器带部署归属标签；单实例沙箱服务启动时只对该部署的遗留容器进行回收 | 在新部署创建一个临时沙箱，强制结束测试沙箱服务，重启后遗留容器被清理 |
| Daytona 依赖准备 | 可配置预制快照；创建时检查 pytest/FastAPI/httpx，缺失时按固定版本安装，安装失败不进入模型执行 | 真实云沙箱完成初始化，实际 pytest 通过，文件/快照验证通过并删除沙箱 |
| 检索连接校验 | DNS 检查通过后连接到检查过的 IP，保留原 HTTP Host 和 TLS 主机名校验；重定向重新校验，不复用跨域 TLS 连接 | 模拟 DNS/重定向测试与真实公开搜索、HTTPS 文档抓取 |
| 监控部署 | Web 指标使用专用 token；沙箱指标使用内部 token；Prometheus 配置凭据文件；Grafana 自动配置数据源和面板 | 三个抓取目标 `ibex-web`、`ibex-worker`、`ibex-sandbox` 均为 `up`；看板 API 返回 200 |

公共页面、API 文档和不含敏感数据的存活/就绪接口可以匿名访问；飞书回调使用平台凭据鉴权，不使用浏览器 API Key。

## 关键实现语义

### 工作区

- 文件按“租户 + 渠道 + 会话”保存，因此相同会话名在不同租户之间相互独立。
- 同一会话已有请求执行时，同步入口返回 409；流式入口返回错误事件；队列执行按有限重试处理，不允许两个写入者互相覆盖。
- 默认上限为 500 个目录/文件条目、8 MB 文件内容。保存前检查容量和符号链接；不保存虚拟环境、Git 内部目录、缓存及提供方隐藏目录。保留 `.github`、`.gitignore`、`.dockerignore`。
- 保存失败会使请求失败并保留上一次完整清单，不把失败保存伪装成成功。
- 持久化对象是项目文件，不是完整容器快照；运行进程、系统包、内存状态、空目录和文件执行权限不在清单中。数据库连接应直连 PostgreSQL 或使用支持会话锁的池，不应改成 PgBouncer transaction pooling。

### 任务与投递

- API 成功接收任务意味着数据库记录已落盘；Redis 暂时不可用时任务保留为 pending，恢复后补发。
- 恢复扫描周期为 15 秒；普通未入队记录至少等待 30 秒；中断执行按 `task_timeout + 60` 秒判定过期。默认执行超时 600 秒。
- `/jobs/{id}` 与 `/tasks/{id}` 都查询数据库结果，不依赖 Redis 结果保留时长。旧的、没有租户归属的历史任务不会自动授权给新租户。
- `/deliveries/{id}` 查询投递状态；`POST /deliveries/{id}/retry` 只允许所属租户重投已耗尽的记录。
- 默认投递尝试 6 次，初始等待 5 秒，指数增长并封顶 300 秒。发送进程中断后由租约到期恢复。
- 飞书发送携带稳定 UUID，降低“平台已接收但本地尚未记账”导致的重复投递。**这是可恢复、可观察的投递流程，不是对外部网络副作用的严格恰好一次保证**。工具执行与跨系统故障仍可能发生重复，不能在简历中写成“端到端 exactly-once”。

## 验收证据

| 本地文件 | 结果 |
| --- | --- |
| `logs/completion-tests.txt` | Windows：99 passed，1 条依赖弃用警告 |
| `logs/completion-linux-tests.txt` | Linux 应用容器：99 passed，2 条依赖弃用警告 |
| `logs/completion-live.json` | 鉴权、重启后文件恢复、跨租户拒绝、真实队列、沙箱崩溃恢复、监控抓取、看板 |
| `logs/completion-browser.json` | 未填写密钥 401，填写后 SSE 200；回复 WEB_AUTH_OK，无页面 JS 错误 |
| `logs/completion-browser.png` | 新部署网页截图，生成前已清空密钥输入框 |
| `logs/completion-search.json` | 3 条真实搜索结果、HTTPS 正文抓取及 IP 固定连接验证 |
| `logs/daytona-service-fixed.json` | Daytona 依赖准备、实际 pytest、文件与只读快照、云资源删除 |

任务执行测试使用真实配置的模型；投递失败恢复测试使用模拟发送器和真实 PostgreSQL，未再次实际发送飞书消息。此前一次飞书收发验收仍作为历史证据，不能说本次新投递逻辑已经重新完成真实飞书收发。

## 启动与复现

在项目目录中运行：

```powershell
$env:PYTHONUTF8 = '1'

# 首次准备：填好模型配置后生成缺失的部署密钥，不会覆盖已有密钥
.\.venv\Scripts\python.exe -m scripts.prepare_deploy

# 初次缺少沙箱镜像时执行
docker build -t ibex-sandbox:local -f sandbox/Dockerfile .

# 当前已运行，不需要重复执行；后续更新代码时使用
docker compose -p ibex-completion -f docker-compose.prod.yml up -d --build

# 本地测试需要 PostgreSQL/Redis；默认读取项目 .env
.\.venv\Scripts\python.exe -m pytest tests tests_service -q --basetemp .pytest-local-run-completion
```

实际部署复测脚本为 `scripts/verify_completion.py`。它会调用模型，并重启自己管理的 `ibex-completion` Web 和沙箱服务以验证恢复行为，应该只在没有其他工作负载的验收部署中运行。`scripts/verify_completion_web.py` 验证真实网页，需要本地 Playwright 和 Edge。Daytona 脚本用 `--config` 指定已授权的凭据文件。

部署端口和密码在 `.env` 中配置。`deploy/secrets/` 由准备脚本生成，被 Git 和 Docker 构建上下文排除。数据库和 Redis 使用持久卷；Redis 启用 AOF。飞书长连接仍是单独 profile，本次未启动。

GitHub Actions 已补 PostgreSQL/Redis 服务并运行同一套回归命令；尚未推送仓库，所以没有远端 Actions 执行结果。

## 结论适用范围

现在可以描述为：**完成鉴权与租户隔离、文件工作区持久化、持久任务提交和独立投递重试，并完成 Docker 多服务部署及故障恢复验证。**

这不等于所有输入都不会失败，也不等于已证明生产 SLA。当前验证使用标准 Docker runtime；gVisor/Kata 未实测；完整模型能力评测及更大的客观题集没有在本轮重跑。API Key 是租户级授权机制，不是企业用户目录或复杂 RBAC 产品。

源代码和累计 `LOCAL_FIXES.patch` 已保存在本地，尚未推送 GitHub。此前两份验收文档保留为历史记录，应以本文判断当前状态。
