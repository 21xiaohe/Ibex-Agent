"""tests/conftest.py —— pytest 共享 fixture

用最小 FastAPI app 装配被测路由，而不是 import 真实的 api.app——
后者 lifespan 强依赖 Postgres/Redis，单元测试不应背着全套基础设施跑。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.orders import router as orders_router


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(orders_router)
    return TestClient(app)
