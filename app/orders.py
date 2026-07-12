from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from decimal import Decimal

router = APIRouter(prefix="/orders", tags=["orders"])

class OrderItem(BaseModel):
    price: Decimal
    quantity: int

class QuoteResponse(BaseModel):
    total: Decimal

@router.post("/quote", response_model=QuoteResponse)
async def create_quote(order_items: list[OrderItem]):
    total = sum(item.price * item.quantity for item in order_items)
    return {"total": total}
