"""Transactional outbox: delivery retries never repeat the agent execution."""
import asyncio
import json
from typing import Any
from uuid import uuid4, uuid5, NAMESPACE_URL

from infra.settings import get_settings


async def finish_task(pool: Any, job_id: str, tenant_id: str, result: dict, target: dict | None) -> dict:
    """Atomically commit the result and a stable delivery identity."""
    result = dict(result)
    delivery_id = uuid5(NAMESPACE_URL, "ibex-delivery:" + tenant_id + ":" + job_id).hex
    if target:
        if target.get("type") != "feishu" or not target.get("chat_id"):
            raise ValueError("Unsupported delivery target")
        result["delivery_id"] = delivery_id
    async with pool.connection() as conn:
        async with conn.transaction():
            if target:
                await conn.execute(
                    "INSERT INTO ibex_outbox(delivery_id,job_id,tenant_id,target,body) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (delivery_id, job_id, tenant_id, json.dumps(target), result["reply"]))
            await conn.execute("UPDATE async_tasks SET status='done',result=%s,error=NULL,updated_at=now() WHERE task_id=%s AND tenant_id=%s",
                               (json.dumps(result), job_id, tenant_id))
    return result


async def send_delivery(target: dict, body: str, delivery_id: str) -> None:
    """Send with a stable provider UUID to reduce duplicate delivery after a crash."""
    if target.get("type") != "feishu":
        raise ValueError("Unsupported delivery target")
    from channels.feishu import FeishuChannel
    await FeishuChannel().send(target["chat_id"], body, message_id=delivery_id)


async def deliver_pending(ctx: dict) -> None:
    """Claim due records with leases, retry with backoff, and retain exhausted records."""
    pool = ctx["pg_pool"]
    settings = get_settings()
    for _ in range(20):
        token = uuid4().hex
        async with pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    "SELECT * FROM ibex_outbox WHERE ((status='pending' AND next_attempt_at<=now()) "
                    "OR (status='sending' AND lease_until<now())) AND (%s::text IS NULL OR tenant_id=%s) ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1", (ctx.get("tenant_id"), ctx.get("tenant_id")))
                row = await cursor.fetchone()
                if row is None:
                    return
                if row["attempts"] >= settings.delivery_max_attempts:
                    await conn.execute("UPDATE ibex_outbox SET status='dead',last_error='DeliveryLeaseExpired',updated_at=now() WHERE delivery_id=%s", (row["delivery_id"],))
                    continue
                await conn.execute("UPDATE ibex_outbox SET status='sending',attempts=attempts+1,lease_until=now()+interval '90 seconds',lease_token=%s,updated_at=now() WHERE delivery_id=%s", (token, row["delivery_id"]))
        try:
            await asyncio.wait_for(send_delivery(row["target"], row["body"], row["delivery_id"]), timeout=60)
        except Exception as error:
            attempt = row["attempts"] + 1
            status = "dead" if attempt >= settings.delivery_max_attempts else "pending"
            delay = min(settings.delivery_retry_delay * 2 ** (attempt - 1), 300)
            async with pool.connection() as conn:
                await conn.execute("UPDATE ibex_outbox SET status=%s,last_error=%s,next_attempt_at=now()+%s*interval '1 second',lease_until=NULL,updated_at=now() WHERE delivery_id=%s AND lease_token=%s",
                                   (status, type(error).__name__, delay, row["delivery_id"], token))
        else:
            async with pool.connection() as conn:
                await conn.execute("UPDATE ibex_outbox SET status='sent',last_error=NULL,lease_until=NULL,updated_at=now() WHERE delivery_id=%s AND lease_token=%s", (row["delivery_id"], token))


async def delivery_status(pool: Any, delivery_id: str, tenant_id: str) -> dict | None:
    """Read only this tenant's delivery state; message content and secrets are omitted."""
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT delivery_id,job_id,status,attempts,last_error,next_attempt_at FROM ibex_outbox WHERE delivery_id=%s AND tenant_id=%s", (delivery_id, tenant_id))
        return await cursor.fetchone()


async def retry_delivery(pool: Any, delivery_id: str, tenant_id: str) -> bool:
    """Explicitly requeue an exhausted delivery without executing the model again."""
    async with pool.connection() as conn:
        cursor = await conn.execute("UPDATE ibex_outbox SET status='pending',attempts=0,next_attempt_at=now(),lease_token=NULL,updated_at=now() WHERE delivery_id=%s AND tenant_id=%s AND status='dead' RETURNING delivery_id", (delivery_id, tenant_id))
        return await cursor.fetchone() is not None
