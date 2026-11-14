"""Tenant-scoped delivery inspection and explicit retry."""
from fastapi import APIRouter, HTTPException, Request
from gateway.auth import tenant_for
from tasks.delivery import delivery_status, retry_delivery

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


@router.get("/{delivery_id}")
async def inspect_delivery(delivery_id: str, request: Request) -> dict:
    """Return a delivery only if it belongs to the caller's tenant."""
    row = await delivery_status(request.app.state.pg_pool, delivery_id, tenant_for(request))
    if row is None:
        raise HTTPException(404, "Delivery not found")
    return row


@router.post("/{delivery_id}/retry")
async def retry(delivery_id: str, request: Request) -> dict:
    """Requeue an exhausted delivery; other states cannot be resent through this route."""
    await inspect_delivery(delivery_id, request)
    if not await retry_delivery(request.app.state.pg_pool, delivery_id, tenant_for(request)):
        raise HTTPException(409, "Only exhausted deliveries can be retried")
    return {"delivery_id": delivery_id, "status": "pending"}
