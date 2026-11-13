"""Compatibility /tasks API backed by the same durable queue as /jobs."""
from fastapi import APIRouter, Request
from api.schemas import TaskRequest
from api.routes.jobs import submit_async, query_job
router = APIRouter(prefix="/tasks", tags=["tasks"])

@router.post("")
async def submit_task(req_body: TaskRequest, request: Request) -> dict:
    """Submit durably so process restarts do not strand BackgroundTasks."""
    result = await submit_async(req_body, request)
    return {"task_id": result["job_id"], "status": result["status"], "conversation_id": result["conversation_id"]}

@router.get("/{task_id}")
async def query_task(task_id: str, request: Request) -> dict:
    """Apply identical tenant authorization to the compatibility endpoint."""
    return await query_job(task_id, request)
