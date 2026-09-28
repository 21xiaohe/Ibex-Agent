"""channels/webhook.py —— 通用 Webhook 接入（含飞书 challenge 验证）

两个工程要点：
① 回调验证：飞书 Webhook 模式首次会发 {"type":"url_verification","challenge":"..."}，
   必须原样返回 challenge（官方要求 1 秒内）；
② 快速返回 + 可靠异步：外部平台会重试，需立即 200 并对重复事件去重（Redis TTL +
   arq job_id 双保险）；消息处理投递给 arq 队列，由独立 worker 执行后回发——
   持久化、失败重试、重启不丢。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.logging import get_logger
from infra.idempotency import seen_before
from obs.metrics import IDEMPOTENCY_HITS
from tasks.queue import enqueue_issue

logger = get_logger()

router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.post("/feishu")
async def feishu_webhook(request: Request):
    """飞书 Webhook 回调模式入口（与长连接二选一）。"""
    body = await request.json()

    # ① challenge 验证:配置回调地址时飞书会先发这个,原样返回 challenge
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    # ② 幂等去重:用 Redis(跨副本共享、带 TTL、重启不丢、原子无竞态)
    header = body.get("header", {})
    event_id = header.get("event_id")
    if event_id and await seen_before(request.app.state.redis, event_id, ttl_seconds=3600):
        IDEMPOTENCY_HITS.inc()  # 埋点：重复事件被挡
        return JSONResponse({"code": 0})  # 重复事件,直接确认

    # ③ 解析消息事件 → 归一化（仅处理 im.message.receive_v1 文本）
    if header.get("event_type") == "im.message.receive_v1":
        import json as _json
        message = body.get("event", {}).get("message", {})
        text = ""
        try:
            text = _json.loads(message.get("content", "{}")).get("text", "").strip()
        except Exception:
            pass
        chat_id = message.get("chat_id", "")
        open_id = body.get("event", {}).get("sender", {}).get("sender_id", {}).get("open_id", "unknown")

        if text:
            payload = {
                "channel": "feishu", "user_id": open_id, "text": text,
                "conversation_id": chat_id, "tenant_id": "default",
                "reply_to": {"type": "feishu", "chat_id": chat_id},
            }
            # ④ 投递 arq 队列（独立 worker 执行：持久化/失败重试/重启不丢），
            #    worker 完成后按 reply_to 把回复发回飞书群。
            #    job_id=event_id：arq 对同 id 重复入队自动去重——与②的 Redis 短路构成双保险。
            await enqueue_issue(request.app.state.redis, payload, job_id=event_id or None)

    # ⑤ 立即 200（飞书 3 秒约束）——不等 agent 跑完
    return JSONResponse({"code": 0})


@router.post("/generic")
async def generic_webhook(request: Request):
    """通用 Webhook：任意外部系统 POST {"text": "...", "source": "..."} 触发 Ibex。

    ⚠️ 注意：本端点是【同步等待】agent 跑完才返回的——适合"任务较快"或调用方
    能接受长等待的场景（如脚本/CI）。一个跑几分钟的 Issue 会让调用方一直阻塞、
    甚至超时。让它"提交即返回、后台异步跑、结果可查"是任务队列的事。
    Web 前端目前调的就是这个端点，所以慢任务时网页会一直转圈——同样待任务队列改善。
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"code": 1, "msg": "empty text"})

    inbound = InboundMessage(
        channel="webhook", user_id=body.get("source", "external"),
        text=text, conversation_id=body.get("conversation_id", "default"), raw=body,
    )
    reply = await handle_message(
        inbound, request.app.state.checkpointer, request.app.state.store
    )
    return JSONResponse({"code": 0, "reply": reply})