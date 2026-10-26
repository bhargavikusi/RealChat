"""Fixtures for tests that need real PostgreSQL and Redis.

Connection details come from TEST_DATABASE_URL and TEST_REDIS_URL (environment or `.env`).
When they are missing the tests are skipped, unless REQUIRE_INTEGRATION=1 is set
(as in CI), in which case missing configuration is a failure.
"""

import os

import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config.settings import Settings


class IntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TEST_", env_file=".env", extra="ignore")

    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None


@pytest.fixture(scope="session")
def integration_settings() -> IntegrationSettings:
    settings = IntegrationSettings()
    if settings.database_url is None or settings.redis_url is None:
        message = "TEST_DATABASE_URL and TEST_REDIS_URL must be set for integration tests"
        if os.environ.get("REQUIRE_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return settings


@pytest.fixture
def database_url(integration_settings: IntegrationSettings) -> str:
    assert integration_settings.database_url is not None
    return integration_settings.database_url.get_secret_value()


@pytest.fixture
def redis_url(integration_settings: IntegrationSettings) -> str:
    assert integration_settings.redis_url is not None
    return integration_settings.redis_url.get_secret_value()


@pytest.fixture
def app_settings(database_url: str, redis_url: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
    )
