"""
infra/settings.py —— Ibex 的配置中枢

- 所有 API Key / Base URL / 连接串统一从这里取，不散落在业务代码里
- Pydantic Settings 做类型校验，缺失关键项时给清晰报错
- 字段前缀 IBEX_（Ibex 缩写），避免与系统其他环境变量撞名
"""
import os
from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "ibex-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    model_config = SettingsConfigDict(
        env_prefix="IBEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== 模型 =====
    model_name: str = "qwen-max"
    model_provider: str = "openai"
    api_key: SecretStr                    # 用 SecretStr，避免密钥被 print/log 泄露
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # ===== 连接弹性 =====
    max_retries: int = 12
    timeout: int = 60

    # ===== 持久化底座 =====
    postgres_url: str = "postgresql://ibex:ibex@localhost:5432/ibex"
    redis_url: str = "redis://localhost:6379/0"

    # ===== 运行 =====
    log_level: str = "INFO"

    mcp_servers: dict[str, dict] = {}

    model_tiers: dict[str, str] = {
        "strong": "qwen-max",  # 难任务：规划、写代码、审查
        "standard": "qwen-plus",  # 中等任务：测试
        "cheap": "qwen-turbo",  # 简单任务：读代码、总结
    }

    # Postgres（持久化 + 连接池）
    database_url: str = "postgresql://ibex:ibex@localhost:5432/ibex"  # 按 compose 改
    pg_pool_min: int = 2
    pg_pool_max: int = 20

    # Redis（备好，后半 arq/限流用）
    redis_url: str = "redis://localhost:6379/0"
    redis_pool_max: int = 20

    # 飞书—— .env 里写 IBEX_FEISHU_*，pydantic-settings 自动映射到下面字段
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_encrypt_key: str = ""  # 长连接可留空；Webhook 回调模式才用
    feishu_verification_token: str = ""  # 同上

    # 网关多租户（给 require_api_key / demo_12_test_gateway / demo_14_test_tenant 用）
    # 形如 {"key-tenant-a": "tenant-a", "key-tenant-b": "tenant-b"}
    api_keys: dict[str, str] = {"key-tenant-a": "tenant-a", "key-tenant-b": "tenant-b"}

    # =====沙箱隔离 =====
    sandbox_provider: str = "docker"  # docker（自托管）/ daytona（外部托管）/ none（本地退化，无沙箱）
    sandbox_runtime: str = "runc"  # runc(加固容器) / runsc(gVisor) / kata(microVM)
    sandbox_image: str = "ibex-sandbox:latest"  # 3.3 会用 Dockerfile 构建这个镜像
    sandbox_workdir: str = "/home/agent"  # 容器内工作目录（tmpfs，可写）
    sandbox_mem_limit: str = "512m"
    sandbox_pids_limit: int = 256
    sandbox_cpus: str = "1.0"
    sandbox_pool_size: int = 4  # 沙箱池并发上限

    # =====任务重试（arq 有限退避重试，只覆盖瞬时执行失败）=====
    task_max_tries: int = 3          # 最多尝试次数（含首次）
    task_retry_delay: float = 2.0    # 首次重试延迟（秒），指数退避，封顶 60s

    # =====并发与限流 =====
    max_concurrent_llm: int = 8  # LLM 并发调用上限

def _export_langsmith_env(s: "Settings") -> None:
    """把 LangSmith 变量写回 os.environ。

    关键：LangSmith 库只认系统环境变量，不读 Settings 对象——所以必须写回 os.environ。
    只在开启 tracing 时写，避免没配 key 时设一堆空变量。
    """
    if not s.langsmith_tracing:
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = s.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = s.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = s.langsmith_endpoint


# ===== 模块级执行：import 这个模块时就把环境变量写好 =====
# 放模块级（不只在 get_settings 里）是为了保证：任何代码一旦 import infra.settings，
# LangSmith 变量就立刻进环境，且通常早于 langchain/langgraph 真正使用追踪。
_settings = Settings()
_export_langsmith_env(_settings)


@lru_cache
def get_settings() -> Settings:
    """全进程单例。业务代码统一通过它拿配置。"""
    return Settings()