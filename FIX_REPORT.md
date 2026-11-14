# Ibex 已确认问题修复与复测记录

> 后续已补齐鉴权、租户权限、持久工作区和投递恢复，并启动新版部署。最新状态见 [COMPLETION_REPORT.md](COMPLETION_REPORT.md)，本文保留为历史记录。

日期：2026-11-13。代码位于本地 `Ibex-Agent` 副本，未推送 GitHub。

本报告记录此前功能验收发现的问题及本次修复结果。`FULL_VERIFICATION.md` 是修复前的历史验收记录，其中“Reviewer 可写”“SSE 缺失”“容器任务 500”等结论应以本报告的复测为准。不能据此声称所有功能、所有输入或生产部署都已验证。

## 1. 修了什么

| 原问题 | 修复后的行为 | 验证证据 |
| --- | --- | --- |
| 团队 Reviewer 与工作角色共用可写环境 | 每次实际调用 Reviewer 时创建最新快照。Docker 使用独立只读容器；Daytona 使用有数量、大小限制的文件快照，拒绝写入且不提供执行工具 | 实际父图调用 `task → reviewer`，强制文件写入、shell 写入均不能改变原工作区；`logs/reviewer-docker-fixed.json` |
| API、团队、MCP、评测的构建路径不一致，Daytona 配置被忽略 | 统一构建入口和资源所有权；Docker、远程 Docker、Daytona 按配置创建并回收；团队由请求的 `team: true` 或 `IBEX_AGENT_TEAM=true` 启用 | pytest 覆盖路由选择、失败清理、实际 MCP 调用；真实 Daytona 创建、执行、文件传输、只读快照、删除通过 |
| 普通执行失败没有明确任务重试 | 对连接失败、超时、模型限流/服务端错误做有上限的退避重试；默认最多 3 次。普通 `RuntimeError` 不盲目重试；稳定消息 ID 避免重试重复追加同一用户消息 | 真实 Redis/arq 中第一次连接失败、第二次成功；上限和非重试错误有 pytest 覆盖；`logs/retry-fixed.json` |
| 应用放进 Docker 后无法运行 Agent 沙箱 | 增加私有沙箱服务和远程后端。仅沙箱服务持有 Docker socket；Web/Worker 使用 token 调用，镜像包含所需 Docker CLI | 容器 API 执行真实命令返回 200；模型生成代码并通过独立验收；`logs/container-fixed.json` |
| `/chat/stream` 不存在，前端只能降级模拟打字 | 新增真实模型增量 SSE；前端解析错误事件；生成结束、异常、断连时回收沙箱 | Edge 浏览器请求 SSE 返回 200，无 JS 错误；ASGI 断连取消测试确认异步清理完成；`logs/browser-verification.json` |
| 搜索、网页抓取只有占位返回 | Bing RSS 搜索返回标题、URL、摘要；网页抓取提取文本，限制响应大小，检查 URL、重定向和非公网地址，失败明确返回错误 | 真实公开搜索和 Python Decimal 文档抓取通过；有解析、私有地址拒绝测试；`logs/search-fixed.json` |
| 两个 Skill 文件为空 | 补齐元信息和 FastAPI、pytest 操作流程 | 测试实际构建 Agent，并检查模型收到的系统提示词已加载两个 Skill |
| 模型 fallback、活跃沙箱指标只有定义 | 计数接入实际降级尝试及沙箱创建/删除；销毁失败明确抛错，重复删除不会把计数减为负数 | pytest 覆盖；最终镜像的远程沙箱、只读快照测试完成后 active 为 0；`logs/broker-final.json` |
| Locust 目录没有场景、没有 CI 工作流 | 增加 readiness 场景及显式启用的真实 Agent 场景；增加 GitHub Actions 回归测试工作流 | 场景行为有测试，Linux 应用镜像内完整回归通过。GitHub 远端工作流尚未运行 |

此前已修好的飞书入队客户端错误、通用 Webhook 漏导入、输入校验、Windows 启动、文件传输、readiness 等修复继续包含在本地代码和补丁中。

本次还修复了两处联调发现的问题：Windows 的数据库事件循环不能直接启动 stdio MCP，现将该类 MCP 的发现与调用桥接到支持子进程的循环；Daytona 用户主目录包含服务自身的隐藏目录，文件快照现排除隐藏元数据（保留 `.github`），避免读到不可遍历的运行环境目录。

## 2. 实测结果及边界

- Windows 回归：**73 passed**；Linux 应用镜像回归：**73 passed**。依赖弃用警告不是失败。最后的快照过滤修改另有针对性测试和真实 Daytona 复测。
- 容器内真实模型编码：创建 `apply_discount`，执行自己的测试，再加入独立验收测试，总计 **16 passed**。这是一次小型任务的客观验收，不代表所有开发任务成功率。
- 真实浏览器：使用 `/chat/stream`，HTTP 200，回复 `IBEX_BROWSER_OK`，无页面 JavaScript 错误。
- 真实队列：生产 `process_issue` 在临时连接故障后第 2 次执行成功，记录一次重试。
- 真实 Docker 团队：Reviewer 的工具写入和命令写入均被阻止；工作区内容未变，临时容器已回收。
- 真实 Daytona：统一后端包装器的文件往返、命令执行、Reviewer 文件快照、删除全部通过；云沙箱已删除。此次未运行 Daytona 多角色真实模型任务，也未重新验证 LangSmith。
- 最终 Docker 镜像：远程执行、最新只读快照、拒绝写入、鉴权指标接口均通过；回收后的活跃沙箱计数为 0。
- Docker Compose 配置校验通过；本次实际启动的是独立 Web/沙箱容器并复用现有本地数据库，未启动整套 Grafana/Prometheus 编排。
- 飞书沿用上一轮已授权并完成的真实收发证据；本轮未再发送消息，相关入队、解析、SDK 发送行为由回归测试覆盖。

主要原始记录：

| 文件 | 内容 |
| --- | --- |
| `logs/fixes-tests.txt` | Windows 完整回归 |
| `logs/linux-tests-fixed.txt` | Linux 镜像完整回归 |
| `logs/fixes-final-extra.txt` | 最后快照修改的针对性回归 |
| `logs/container-fixed.json` | 实际容器 API、socket 检查、模型编码及独立测试 |
| `logs/container-coding-fixed.txt` | 编码任务执行过程 |
| `logs/broker-final.json` | 最终镜像的远程执行、只读快照与回收计数 |
| `logs/reviewer-docker-fixed.json` | 实际团队 Reviewer 强制写入验证 |
| `logs/daytona-service-fixed.json` | Daytona 统一后端与快照实测 |
| `logs/retry-fixed.json` | 真实 arq 第二次执行恢复 |
| `logs/search-fixed.json` | 公开网站搜索与正文抓取 |
| `logs/browser-verification.json` | 浏览器 SSE 请求及页面检查 |

## 3. 如何使用

本地 API 仍使用 `http://127.0.0.1:18700`，接口文档是 `/docs`。现有服务在最终几处代码修改前启动，需要重启才能加载完整修复。自动审批拒绝了停止并重启本轮 API/Worker 的命令，仅返回 `blocked by policy`，因此没有继续停止进程。当前 `logs/local-services.json` 保存的是启动器 PID；可用 `Get-NetTCPConnection -LocalPort 18700,8002 -State Listen` 查询实际监听进程。不要在现有服务运行时再启动相同端口。

手动关闭原 API/Worker 后，分别运行 `python -m scripts.run_local web` 和 `python -m scripts.run_local worker`（使用项目虚拟环境）。修复后的应用镜像 `ibex-app:fixed` 已完成独立验证。

```powershell
# 在 Ibex-Agent 目录运行
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest tests tests_service -q --basetemp .pytest-local-run-manual

# 启用团队模式的请求示例（会调用模型）
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18700/issues' `
  -ContentType 'application/json' `
  -Body '{"issue":"Review the current project and identify one concrete defect.","team":true}'
```

Docker 部署前，先构建沙箱镜像：

```bash
docker build -t ibex-sandbox:local -f sandbox/Dockerfile .
docker compose -f docker-compose.prod.yml up -d --build web worker
```

`.env` 必须填好模型配置、`IBEX_PG_PASSWORD` 和随机生成的 `IBEX_SANDBOX_API_TOKEN`。Compose 会覆盖数据库/Redis 地址为容器服务名，并把 `IBEX_SANDBOX_API_URL` 指向私有沙箱服务；本地进程模式该 URL 留空即可。沙箱服务不需要真实模型密钥。

Web 端口默认只绑定本机回环。飞书是独立的 `feishu` profile，不随普通 Web/Worker 启动。私有沙箱服务没有发布宿主机端口。`IBEX_SANDBOX_IDLE_TIMEOUT` 控制闲置资源回收，默认 1800 秒；只读审查也占用沙箱容量。

复测脚本：`scripts/verify_retry_fixed.py`、`scripts/verify_reviewer_fixed.py`、`scripts/verify_container_fixed.py`、`scripts/verify_daytona_service.py`、`scripts/verify_web.py`。容器编码与浏览器脚本会调用模型；Daytona 脚本需要用 `--config` 指定本机凭据文件。不要在聊天或版本库中放密钥。

Locust 默认只访问 readiness；设置 `IBEX_LOAD_AGENT=1` 和 `IBEX_LOAD_API_KEY` 后才会调用模型。可用 `locust -f loadtest/locustfile.py --host http://127.0.0.1:18700` 启动。这里没有给出吞吐量或并发性能结论。

## 4. 仍然不能宣称完成的事项

1. **全入口鉴权、多租户资源权限和持久工作区**：`/issues/secure` 已验证，但其他入口并没有自动变成完整的生产鉴权系统；不同请求会新建工作沙箱，历史对话不等于文件工作区持久化。当前适合本地或受控环境演示。
2. **恰好一次执行**：任务重试会重跑执行逻辑，不保证所有工具副作用只发生一次。发送失败不触发整任务重跑，但也没有独立的可靠消息投递重试系统。模型调用重试、fallback、队列重试是不同层次。
3. **强隔离与多租户沙箱平台**：标准 Docker 加固与只读快照已经实测；gVisor/Kata 未实测。沙箱服务持有 Docker socket，属于可信控制组件；当前 token 面向内部应用，不是每个终端租户独立的授权系统。进程被强制终止后的孤儿容器对账仍需补充。
4. **Daytona 开发依赖**：默认云快照不保证带 pytest/FastAPI/httpx。此前验收曾临时安装依赖；本次没有把安装操作隐式塞进每个任务。真实开发任务仍需准备带依赖的云环境。
5. **检索质量与完整网络防护**：RSS 搜索结果不保证相关性或长期稳定；抓取检查公网地址及重定向，但未实现 DNS 地址固定，不能称为完整 DNS 重绑定防护。正文长度受限，不支持所有网页/PDF。
6. **评测与发布门禁**：没有重新跑全量模型裁判评测，也没有把历史分数当成修复后提升。GitHub Actions 文件已写好，远端尚未运行；客观题集扩充、有效基线和模型质量门禁仍是后续工作。
7. **监控部署**：fallback/沙箱指标已接入执行代码；沙箱服务的 `/metrics` 需要 Bearer token，Prometheus 还需配置对应凭据和抓取任务。未宣称完整看板已经部署。

## 5. 代码交付

本地修改已写入项目，`LOCAL_FIXES.patch` 汇总相对原审阅版本 `174daf23d3ea63e06a4c7f24583aeacc5c059f75` 的累计改动，并用原副本做 `git apply --check`。补丁不包含 `.env`、日志、虚拟环境和模型密钥。

GitHub 仓库当前不会自动包含这些修复；后续应在对应仓库中审阅、应用、提交再推送。若远端代码已变化，先解决差异并重新测试，不能直接假定补丁仍适用。
