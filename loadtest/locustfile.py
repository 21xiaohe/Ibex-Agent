"""loadtest/locustfile.py —— Locust 压测：分位数延迟分析 + 容量规划

用法（对本地活服务压 /readyz，30 秒、20 并发）：
    uv run python -m locust -f loadtest/locustfile.py \
        --host http://127.0.0.1:18704 --users 20 --run-time 30s --headless

压真实业务端点 /issues（每次请求都是一次真实 LLM 调用，慢且花钱，慎用）：
    set IBEX_API_KEY=<容器内 IBEX_API_KEYS 里的 key>
    uv run python -m locust -f loadtest/locustfile.py \
        --host http://127.0.0.1:18704 --users 5 --run-time 60s --headless \
        --tags issue

设计说明：
- 健康检查任务（readyz）默认开启，测的是网关/FastAPI 链路的吞吐与延迟分布；
- /issues 任务默认关闭（@tag issue），因为它是真实 LLM 调用——压测要看的是
  服务编排层容量，不是把 token 费烧穿；要压业务端点时用 --tags issue 显式打开。
- API Key 从环境变量 IBEX_API_KEY 读；压 readyz 不需要 key。
"""
import os

from locust import HttpUser, between, task


class IbexUser(HttpUser):
    # 每个虚拟用户两次请求间思考 0.2~1s，模拟真实用户节奏
    wait_time = between(0.2, 1.0)

    def on_start(self):
        # 服务开着网关鉴权（X-API-Key）；压 readyz 用不到，压 /issues 必须带
        key = os.environ.get("IBEX_API_KEY", "")
        if key:
            self.client.headers["X-API-Key"] = key

    @task
    def readyz(self):
        """健康检查：轻请求，测编排链路本身的吞吐与分位数延迟。"""
        self.client.get("/readyz")

    @task
    def healthz(self):
        self.client.get("/healthz")

    # 真实业务端点：一次完整 Agent 调用（LLM + 沙箱），默认不注册任务——
    # 只有显式设 IBEX_LOAD_ISSUES=1 时才挂进压测，防止默认跑起来把 token 费烧穿
    if os.environ.get("IBEX_LOAD_ISSUES"):

        @task
        def issues(self):
            self.client.post(
                "/issues",
                json={"issue": "回复一句话：收到。", "user_id": "loadtest"},
            )
