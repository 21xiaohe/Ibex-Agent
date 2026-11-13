"""Fixtures for the sample project seeded into task sandboxes."""
from collections.abc import Iterator
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.orders import router


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Serve the sample order API without starting infrastructure services."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
