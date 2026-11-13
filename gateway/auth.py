"""gateway/auth.py —— 网关鉴权（API Key 示例 + 多租户上下文）

用 FastAPI 依赖注入：在路由上声明 Depends(require_api_key)，
未带或错误的 key 直接 401，不进入业务。
"""
from fastapi import Header, HTTPException, status, Request

from infra.settings import get_settings
import hmac


async def require_api_key(x_api_key: str | None = Header(None)) -> str:
    """校验请求头 X-API-Key。返回它对应的租户标识（这里简化为 key 本身）。

    生产里：key → 租户的映射查数据库/配置；这里用 settings 里配的允许列表演示。
    """
    s = get_settings()
    allowed = s.api_keys  # dict: {api_key: tenant_id}
    if not x_api_key or x_api_key not in allowed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 API Key",
        )
    return allowed[x_api_key]   # 返回租户 id，供下游使用


async def authorize_request(request: Request, x_api_key: str | None = Header(None)) -> None:
    """Protect all application routes; Feishu uses its own verified callback credentials."""
    if request.url.path in {"/", "/healthz", "/readyz", "/webhook/feishu"}:
        return
    metrics_token = get_settings().metrics_token.get_secret_value()
    if request.url.path == "/metrics" and metrics_token and hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + metrics_token):
        return
    request.state.tenant_id = await require_api_key(x_api_key)


def tenant_for(request: Request) -> str:
    """Read server-established identity; never use tenant fields supplied in a request body."""
    tenant = getattr(request.state, "tenant_id", None)
    if tenant is None:
        raise HTTPException(401, "Authentication required")
    return tenant
