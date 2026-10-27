"""API checks that need no running services.

Creating the engine and Redis client does not open connections, so these tests
run without PostgreSQL or Redis as long as no endpoint touches them.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.main import create_app
from backend.config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=SecretStr("postgresql+asyncpg://user:unused@127.0.0.1:1/unused"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
    )
    return TestClient(create_app(settings))


def test_health_does_not_depend_on_services(client: TestClient) -> None:
    with client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_docs_are_not_exposed(client: TestClient) -> None:
    with client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
