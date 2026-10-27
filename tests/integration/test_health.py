from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.main import create_app
from backend.config.settings import Settings

# Nothing listens on port 1, so connections are refused immediately.
UNREACHABLE_PASSWORD = "leak-check-pw-5c1"
UNREACHABLE_DATABASE_URL = f"postgresql+asyncpg://realchat:{UNREACHABLE_PASSWORD}@127.0.0.1:1/x"
UNREACHABLE_REDIS_URL = f"redis://:{UNREACHABLE_PASSWORD}@127.0.0.1:1/0"


def _with(settings: Settings, **overrides: str) -> Settings:
    return settings.model_copy(update={key: SecretStr(value) for key, value in overrides.items()})


def test_health_and_ready_when_services_are_up(app_settings: Settings) -> None:
    with TestClient(create_app(app_settings)) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


def test_not_ready_when_database_is_unreachable(app_settings: Settings) -> None:
    settings = _with(app_settings, database_url=UNREACHABLE_DATABASE_URL)

    with TestClient(create_app(settings)) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable", "redis": "ok"},
    }
    assert UNREACHABLE_PASSWORD not in ready.text
    assert "127.0.0.1" not in ready.text


def test_not_ready_when_redis_is_unreachable(app_settings: Settings) -> None:
    settings = _with(app_settings, redis_url=UNREACHABLE_REDIS_URL)

    with TestClient(create_app(settings)) as client:
        ready = client.get("/ready")

    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "redis": "unavailable"},
    }
    assert UNREACHABLE_PASSWORD not in ready.text
