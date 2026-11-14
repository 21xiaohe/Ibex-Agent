"""Durable submission and tenant-scoped result lookup."""
from uuid import uuid4
from fastapi import APIRouter, Request, HTTPException
from gateway.auth import tenant_for
from gateway.limiter import limiter
from api.schemas import TaskRequest
from tasks.queue import submit_job
from tasks.store import get_task
router = APIRouter(prefix="/jobs", tags=["jobs"])

@router.post("")
@limiter.limit("10/minute")
async def submit_async(req_body: TaskRequest, request: Request) -> dict:
    """Persist a task and publish it to the worker queue."""
    payload = {**req_body.model_dump(), "tenant_id": tenant_for(request),
               "conversation_id": req_body.conversation_id or uuid4().hex}
    job_id = await submit_job(request.app.state.pg_pool, request.app.state.arq_redis, payload)
    return {"job_id": job_id, "status": "accepted", "conversation_id": payload["conversation_id"]}

@router.get("/{job_id}")
async def query_job(job_id: str, request: Request) -> dict:
    """Return only the caller's persisted result, even after queue result expiry."""
    row = await get_task(request.app.state.pg_pool, job_id, tenant_for(request))
    if row is None:
        raise HTTPException(404, "Job not found")
    return {**row, "job_id": row["task_id"]}
