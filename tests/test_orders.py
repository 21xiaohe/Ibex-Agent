from fastapi.testclient import TestClient
from app.orders import router, OrderItem, QuoteResponse
import pytest

def test_create_quote_with_multiple_items(client):
    # 注意：端点签名的 body 是裸 list[OrderItem]；price 传字符串由 pydantic 解析成 Decimal
    # （不能用 item.dict() 直接当 json 载荷——里面是 Decimal，httpx 编码不了）
    order_items = [
        {"price": "10.5", "quantity": 2},
        {"price": "5.25", "quantity": 3},
    ]
    response = client.post("/orders/quote", json=order_items)
    assert response.status_code == 200
    # 10.5*2 + 5.25*3 = 21 + 15.75 = 36.75（原断言 37.25 是算错的期望值）
    assert QuoteResponse(**response.json()) == QuoteResponse(total="36.75")

# Add more tests for edge cases and error paths
