"""api/routes/tasks_bg.py —— 方案一：BackgroundTasks 异步任务（提交 + 查询）

提交：写一条 pending 记录 → 立即返回 task_id → BackgroundTasks 后台跑 agent → 更新记录。
查询：拿 task_id 查任务表。
"""
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Request

from agent.main import build_agent
from channels.base import InboundMessage
from channels.handler import handle_message
from tasks.store import create_task, update_task, get_task
from infra.logging import get_logger

logger = get_logger()
router = APIRouter(prefix="/tasks", tags=["tasks-bg"])


async def _run_task(pool, checkpointer, store, task_id: str, payload: dict):
    """后台真正跑 agent 的函数（BackgroundTasks 在响应返回后执行它）。"""
    try:
        await update_task(pool, task_id, status="running")
        inbound = InboundMessage(
            channel=payload["channel"], user_id=payload["user_id"],
            text=payload["text"], conversation_id=payload["conversation_id"],
        )
        reply = await handle_message(inbound, checkpointer, store)
        await update_task(pool, task_id, status="done", result={"reply": reply})
        logger.info("后台任务完成：{}", task_id)
    except Exception as e:  # noqa: BLE001
        await update_task(pool, task_id, status="error", error=str(e))
        logger.exception("后台任务失败：{}", task_id)


@router.post("")
async def submit_task(req_body: dict, request: Request, background: BackgroundTasks):
    """提交任务：立即返回 task_id（不等它跑完）。"""
    task_id = uuid4().hex
    payload = {
        "channel": req_body.get("channel", "api"),
        "user_id": req_body.get("user_id", "anonymous"),
        "text": req_body["text"],
        "conversation_id": req_body.get("conversation_id", task_id),
    }
    pool = request.app.state.pg_pool
    await create_task(pool, task_id, payload["channel"], payload)   # 先写 pending 记录

    # BackgroundTasks：响应返回后才执行（由框架管理，比游离 create_task 正规）
    background.add_task(
        _run_task, pool, request.app.state.checkpointer,
        request.app.state.store, task_id, payload,
    )
    return {"task_id": task_id, "status": "pending"}   # 立即返回


@router.get("/{task_id}")
async def query_task(task_id: str, request: Request):
    """查任务状态/结果。"""
    row = await get_task(request.app.state.pg_pool, task_id)
    if row is None:
        return {"task_id": task_id, "status": "not_found"}
    return row