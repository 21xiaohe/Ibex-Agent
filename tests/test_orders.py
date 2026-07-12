from fastapi.testclient import TestClient
from app.orders import router, OrderItem, QuoteResponse
import pytest

def test_create_quote_with_multiple_items(client):
    order_items = [
        OrderItem(price="10.5", quantity=2),
        OrderItem(price="5.25", quantity=3)
    ]
    response = client.post("/orders/quote", json={"order_items": [item.dict() for item in order_items]})
    assert response.status_code == 200
    assert QuoteResponse(**response.json()) == QuoteResponse(total="37.25")

# Add more tests for edge cases and error paths
