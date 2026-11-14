"""Versioned service tables, independent of LangGraph checkpoint internals."""
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS ibex_workspaces (
 tenant_id TEXT NOT NULL, thread_id TEXT NOT NULL, files JSONB NOT NULL DEFAULT '{}',
 version BIGINT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id, thread_id));
CREATE TABLE IF NOT EXISTS ibex_outbox (
 delivery_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
 target JSONB NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 lease_until TIMESTAMPTZ, lease_token TEXT, last_error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ibex_outbox_due ON ibex_outbox(status,next_attempt_at);
"""


async def init_durable_tables(pool: Any) -> None:
    """Apply additive migrations; legacy tasks remain inaccessible to ordinary tenants."""
    from tasks.store import init_task_table
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(723491085)")
            await init_task_table(pool, connection=conn)
            await conn.execute(DDL, prepare=False)
