# Ibex

最新运行、鉴权、持久工作区、投递恢复和完整部署验收见 [COMPLETION_REPORT.md](COMPLETION_REPORT.md)。本地新版网页为 http://127.0.0.1:18704/ ，所有业务 API 现在需要 `X-API-Key`；下文旧示例调用时也需添加该请求头。

固定开发任务集与客观验收方法见 [eval/OBJECTIVE_EVAL.md](eval/OBJECTIVE_EVAL.md)。自动 CI 验证服务及评测器；真实模型评测通过手动工作流运行，不把单元测试通过率当成 Agent 任务成功率。

> 一个开源、可自部署、多渠道接入的 **Dev 场景 Agent 工程框架**
>
> 把不可控的大模型，套进可控的 **Harness 工程骨架**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.0+-1C3D5A)](https://langchain-ai.github.io/langgraph/)
[![DeepAgents](https://img.shields.io/badge/DeepAgents-0.6.x-5C8D76)](https://github.com/langchain-ai/deepagents)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 项目简介

大模型的能力是"不稳定资产"：同一个 Issue，今天能规划→改码→跑测试一条龙，明天就 hallucinate 出一个不存在的接口。**Ibex 想解决的不是"Agent 能不能干活"，而是"敢不敢让它在你机器上干活"**——所以整个项目围绕一条工程红线展开：

> **主进程调度、沙箱执行、密钥绝不进沙箱。**

Ibex 基于 **DeepAgents + LangGraph** 构建，以 **Agent Harness（智能体工程骨架）** 为核心理念，落地了一个真实可用的研发助手：它能在隔离沙箱里看代码、改 Issue、跑测试，并通过飞书 / Web / CLI / Webhook 多渠道为团队提供服务，覆盖 Agent 工程从 **构建 → 隔离 → 多渠道接入 → 异步化 → 多租户治理 → 可观测 → 评估 → 压测 → 部署上线** 的完整生产链路。

## 核心特性

### Agent 工程骨架（Harness）

- **主 Agent + 多 Subagent 协作**：Coder / Reviewer / Researcher 分工，基于 DeepAgents 原生路由
- **AgentMiddleware 中间件链**：请求上下文 → 工具审计 → 成本计量 → LLM 并发限制 → 模型 fallback
- **物理只读 Reviewer**：`FilesystemPermission` 机制层强约束，**不靠 prompt 自律**

### 沙箱隔离三档可插拔

| 档位 | 技术 | 适用场景 |
|---|---|---|
| **L1 加固容器** | `runc` + 断网/只读/丢能力/非 root | 本地开发、团队内部 |
| **L2 gVisor** | `runsc` 用户态内核 | 多租户、有限可信 |
| **L3 microVM** | `kata` + Firecracker | 生产、完全不可信代码 |

**换一个 `--runtime` 配置即换隔离级别，上层代码不动。**

### 多渠道统一接入

| 渠道 | 实现 | 适用 |
|---|---|---|
| 🛰 **飞书** | lark-oapi 长连接 / Webhook 双模式 | 团队协作 |
| 🌐 **Web** | 零依赖单页面前端（流式 UI、代码块渲染） | 浏览器直接对话 |
| 💻 **CLI** | 命令行客户端 | 开发者、CI 集成 |
| 🪝 **Webhook** | 通用 HTTP 接收 | GitHub Issue / 监控告警等外部事件 |

**渠道差异收敛在适配器，业务核心只认 `InboundMessage` 统一请求。**

### 异步任务编排（双方案）

- **方案一**：`BackgroundTasks + 任务状态表`——轻量、零新增组件
- **方案二**：`arq + Redis` 队列——生产级、独立扩展 worker、可靠重试
- **HITL 人在回路**：`interrupt_on + Command(resume)` 实现危险动作人工审批

### API 网关与多租户治理

- API Key 鉴权 + 租户身份贯穿到 `thread_id` 派生层
- SlowAPI 按租户分桶限流（Redis 多副本共享）
- Redis `SETNX + TTL` 原子幂等去重

### 完整可观测体系

- **两层指标埋点**：HTTP 层 RED + Agent 层成本/Token/任务耗时
- **Prometheus 多进程聚合**：正确处理 Gunicorn 多 worker 指标
- **LangSmith 追踪**：Agent 内部决策链路全可见

### 质量保障闭环

- **Eval 评估**：客观评分（沙箱 pytest 0/1）+ LLM 裁判 + 方差处理（均值±std）
- **可作 CI 质量门禁**：Eval 退化即拒绝发版
- **Locust 压测**：分位数延迟分析 + 容量规划

## 🏗 项目架构

```
┌──────────────────────────────────────────────────────────────┐
│  接入层 │ 飞书长连接 │ Web 前端 │ CLI │ Webhook（外部事件）            │
└──────────┬───────────┴──────────┴────────────────────────────┘
           │ ChannelAdapter（统一 InboundMessage）
           ▼
┌──────────────────────────────────────────────────────────────┐
│  网关层 │ API Key 鉴权 → tenant 上下文 → 限流 → 幂等去重         │
└──────────┬───────────────────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────┐
│  会话层 │ thread_id_for(tenant, channel, conv) → 持久化         │
└──────────┬───────────────────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────┐
│  Harness 骨架 │ AgentMiddleware 链                            │
│   ├─ RequestContext  ├─ ToolAudit  ├─ CostMeter              │
│   ├─ LLMConcurrency  └─ ModelFallback                        │
└──────────┬───────────────────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────┐
│  主 Agent（DeepAgents）│ 路由 → 调度 Coder/Reviewer/Researcher │
└──────────┬───────────────────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────┐
│  沙箱层（BaseSandbox 接口）                                     │
│  L1 加固容器 │ L2 gVisor │ L3 microVM                          │
│  断网 / 只读 / 丢能力 / 非 root / 用完即弃                       │
└──────────┬───────────────────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────┐
│  异步执行 │ arq Worker（独立进程）+ Redis 队列                  │
│            HITL 中断点（interrupt_on + resume）                │
└──────────┬───────────────────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────┐
│  可观测 │ Prometheus │ Grafana │ LangSmith │ Eval │ Locust    │
└──────────────────────────────────────────────────────────────┘
```

## 📁 项目结构

```
ibex-agent/
├── api/                    # FastAPI 入口、路由、lifespan
│   ├── routes/             # issues / jobs / tasks_bg / webhook
│   └── schemas.py
├── agent/                  # 主 Agent 构建（DeepAgents）+ 团队任务编排
├── channels/               # 多渠道适配器（飞书 / CLI / Webhook / 会话派生）
├── web/                    # 零依赖单页面前端（对接 /webhook/generic）
├── sandbox/                # 沙箱抽象与实现
│   ├── docker_sandbox.py   # 加固容器实现（BaseSandbox）
│   ├── manager.py          # 生命周期管理
│   ├── pool.py             # 沙箱池
│   └── Dockerfile          # 沙箱镜像
├── subagents/              # subagent 定义（Reviewer 物理只读等）
├── tasks/                  # 异步任务（状态表 / arq producer / worker）
├── gateway/                # 网关（API Key 鉴权 / 限流）
├── middleware/             # AgentMiddleware（审计 / 成本 / 上下文 / 并发）
├── obs/                    # Prometheus 多进程指标 + LangSmith 初始化
├── infra/                  # settings / 连接池 / llm_router / 幂等 / fallback
├── profiles/               # Harness Profile 注册（多模型路由）
├── eval/                   # 评估数据集 + 沙箱执行评分 + LLM 裁判
├── loadtest/               # Locust 压测
├── scripts/                # demo_01 ~ demo_24 分步验证脚本
├── app/                    # 内置示例项目（订单微服务，seed 进沙箱供 Agent 操作）
├── tests/                  # 示例项目单元测试（Agent 跑测试的靶子）
├── skills/                 # Skills（fastapi-endpoint / unit-test）
├── memory/                 # Agent 长期记忆文件
├── deploy/                 # 部署资产（prometheus.yml）
├── Dockerfile              # 应用镜像
├── docker-compose.prod.yml # 生产编排（web/worker/feishu/pg/redis/prometheus/grafana）
├── requirements.txt        # pip 部署依赖（国内服务器友好）
├── pyproject.toml          # 本地开发（uv）
└── .env.example
```

## 快速开始

### 环境要求

- Python 3.11+
- Docker & Docker Compose
- PostgreSQL 17（含 pgvector）+ Redis 7+（compose 自动起）
- 一个 LLM API Key（默认阿里千问 Qwen）

### 本地开发

```bash
# 1. 克隆项目
git clone https://github.com/21xiaohe/ibex-agent.git
cd ibex-agent

# 2. 装依赖（本地用 uv）
uv sync

# 3. 准备配置
cp .env.example .env
# 编辑 .env，填上 IBEX_API_KEY（你的 LLM 密钥）等

# 4. 起依赖（PG + Redis）
docker compose -f docker-compose.prod.yml up -d postgres redis

# 5. 起服务
uv run uvicorn api.app:app --reload --port 8000

# 6. （另开终端）起 arq worker
uv run arq tasks.worker.WorkerSettings

# 7. 试一下
curl -X POST http://localhost:8000/webhook/generic \
  -H "Content-Type: application/json" \
  -d '{"text":"用一句话解释幂等性","source":"cli","conversation_id":"demo"}'
```

也可以跑分步验证脚本（`scripts/demo_01` ~ `demo_24`，从第一次对话到沙箱切换逐环验证）：

```bash
uv run python scripts/demo_01_try_ibex.py
```

### 生产部署

```bash
# 1. 上传项目到服务器
cd /opt/ibex-agent

# 2. 配置 .env（host 用 compose 服务名 postgres/redis，不是 localhost）
sed -i 's#@localhost:5432#@postgres:5432#' .env
sed -i 's#redis://localhost:6379#redis://redis:6379#' .env
echo "IBEX_PG_PASSWORD=ibex" >> .env

# 3. 配 Docker 镜像加速器（国内服务器必须）
sudo tee /etc/docker/daemon.json <<-'EOF'
{ "registry-mirrors": ["https://docker.m.daocloud.io", "https://docker.1ms.run"] }
EOF
sudo systemctl restart docker

# 4. 起全套（web + worker + feishu + pg + redis + prometheus + grafana）
sudo docker compose -f docker-compose.prod.yml up -d --build

# 5. 验证
curl http://localhost:18700/readyz
```

### 访问入口

| 服务 | 地址 | 说明 |
|---|---|---|
| API 文档 | `http://服务器IP:18700/docs` | FastAPI Swagger |
| Grafana | SSH 隧道 `localhost:13000` | 监控看板，admin/admin |
| Prometheus | SSH 隧道 `localhost:19090` | 指标抓取后台 |

SSH 隧道（监控只绑 `127.0.0.1`，不裸奔公网）：

```bash
ssh -L 19090:127.0.0.1:19090 -L 13000:127.0.0.1:13000 root@服务器IP
```

## 三种使用方式

### 1. Web（浏览器对话）

```bash
# 直接用浏览器打开 web/index.html，右上角填后端地址（默认 http://localhost:8000）
# 无需任何构建工具：纯 HTML + 原生 JS，后端已配 CORS
```

### 2. CLI（开发者 / 自动化）

```bash
# 用法一：直接 curl（bash）
curl -X POST http://服务器IP:18700/webhook/generic \
  -H "Content-Type: application/json" \
  -d '{"text":"...","source":"cli","conversation_id":"cli:demo"}'

# 用法二：channels/cli.py 脚本（同 --session 续聊）
uv run python -m channels.cli --session mywork "用一句话解释幂等性"
```

### 3. Webhook（外部事件驱动）

任意外部系统 POST `{"text": "...", "source": "..."}` 即可触发，天然适配 GitHub Issue 回调、监控告警等场景；内置事件 id 幂等去重。

### 4. 飞书（团队协作）

在飞书开放平台创建应用机器人，在 `.env` 填上 `IBEX_FEISHU_APP_ID` / `IBEX_FEISHU_APP_SECRET`，重启 feishu 服务：

```bash
sudo docker compose -f docker-compose.prod.yml restart feishu
```

在飞书群 @ 机器人，它会回复你。

## 监控

compose 自带 Prometheus + Grafana：HTTP 层 QPS / 错误率 / 延迟 P99，任务层耗时分位数 / 排队深度，沙箱活跃数 / 创建耗时 P95，按 `deploy/prometheus.yml` 抓取配置开箱即用。

## 技术栈

| 类别 | 选型 |
|---|---|
| 开发语言 | Python 3.11+ |
| Agent 框架 | DeepAgents 0.6.x + LangGraph 1.0+ |
| LLM 模型 | Qwen-max / plus / turbo（多档 fallback） |
| Web 框架 | FastAPI + Uvicorn |
| 应用服务器 | Gunicorn + UvicornWorker |
| 数据库 | PostgreSQL 17（pgvector）+ Redis 7（redis-stack） |
| 任务队列 | arq（async-native） |
| 沙箱隔离 | Docker + runc / gVisor / Kata |
| API 网关 | FastAPI Depends + SlowAPI |
| 飞书 SDK | lark-oapi |
| 数据验证 | Pydantic 2.x |
| 监控指标 | Prometheus（multiprocess） |
| 监控看板 | Grafana |
| 追踪 | LangSmith |
| 压测 | Locust |
| 日志 | Loguru |
| 容器编排 | Docker Compose |
| 包管理 | uv（本地）/ pip（部署镜像，国内服务器友好） |

## 设计哲学

贯穿整个项目的几条工程信念：

1. **机制优先于提示词**：Reviewer 只读不靠"你不要改"的 prompt，靠 `FilesystemPermission` deny-write 机制
2. **密钥绝不进沙箱**：主进程持有密钥，沙箱拿到的是短期 token 或代理后的接口
3. **Eval 是 CI 的质量门禁**：不能量化的能力等于不存在；Agent 退化必须能在发版前被拦下
4. **先单进程做对，再多副本扩展**：不要为了"显得分布式"上 RabbitMQ/K8s；Redis + arq 足够支撑你需要的复杂度
5. **可观测的两层视角**：HTTP 接入层和 Agent 执行层各自有指标，没有这两层切分就没法定位瓶颈
6. **渠道差异收敛在适配器**：核心业务只认 `InboundMessage`，加新渠道一行业务代码不改

## Roadmap

- [ ] Grafana Dashboard as Code（随项目附带 JSON，一键导入）
- [ ] L2 gVisor / L3 Kata 沙箱档位实测文档
- [ ] Eval 质量门禁接入 GitHub Actions

## License

MIT
