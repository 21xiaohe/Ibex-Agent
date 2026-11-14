"""Authenticate callback payloads using the installed Feishu SDK's wire format."""
import hashlib
import hmac
import json
import time
from fastapi import HTTPException, Request
from infra.settings import get_settings


async def verified_event(request: Request) -> dict:
    """Reject unconfigured, invalid, stale or mismatched callbacks before queue access."""
    settings = get_settings()
    token, key = settings.feishu_verification_token, settings.feishu_encrypt_key
    if not token:
        raise HTTPException(503, "Feishu callback authentication is not configured")
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413, "Callback too large")
    try:
        body = json.loads(raw)
        if body.get("encrypt"):
            if not key:
                raise ValueError("Missing encryption key")
            from lark_oapi.core.utils import AESCipher
            body = json.loads(AESCipher(key).decrypt_str(body["encrypt"]))
        header = body.get("header", {})
        actual = body.get("token", header.get("token", ""))
        if not isinstance(actual, str) or not hmac.compare_digest(actual.encode(), token.encode()):
            raise ValueError("Token mismatch")
        if body.get("type") != "url_verification":
            if settings.feishu_app_id and header.get("app_id") != settings.feishu_app_id:
                raise ValueError("Application mismatch")
            if key:
                stamp = request.headers.get("X-Lark-Request-Timestamp", "")
                nonce = request.headers.get("X-Lark-Request-Nonce", "")
                signature = request.headers.get("X-Lark-Signature", "")
                expected = hashlib.sha256((stamp + nonce + key).encode() + raw).hexdigest()
                if not nonce or abs(time.time() - int(stamp)) > 300 or not hmac.compare_digest(expected, signature):
                    raise ValueError("Signature mismatch or stale request")
        return body
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(401, "Invalid Feishu callback") from None


def event_job_id(event_id: str) -> str:
    """Namespace stable provider IDs by the configured app and tenant."""
    settings = get_settings()
    return "feishu-" + hashlib.sha256(json.dumps([settings.feishu_tenant_id, settings.feishu_app_id, event_id]).encode()).hexdigest()
