"""Fixtures for tests that need real PostgreSQL and Redis.

Connection details come from TEST_DATABASE_URL and TEST_REDIS_URL (environment or `.env`).
When they are missing the tests are skipped, unless REQUIRE_INTEGRATION=1 is set
(as in CI), in which case missing configuration is a failure.
"""

import os
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.config.settings import BotSettings, Settings
from backend.core.redis import create_redis
from backend.database.engine import create_engine, create_session_factory
from tests.support.chat import Chat
from tests.support.telegram import FAKE_TOKEN


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


@pytest.fixture(scope="session")
def migrated_database_url(integration_settings: IntegrationSettings) -> str:
    """The test database, migrated to the latest schema once per test session."""
    assert integration_settings.database_url is not None
    url = integration_settings.database_url.get_secret_value()
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def db_engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        # Each test starts from empty tables. CASCADE covers dependent tables.
        await connection.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(db_engine)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    # TEST_REDIS_URL points at a database used only by tests, so flushing it is safe.
    client = create_redis(redis_url)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def bot_settings(database_url: str, redis_url: str) -> BotSettings:
    return BotSettings(
        _env_file=None,
        app_env="test",
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
        telegram_bot_token=SecretStr(FAKE_TOKEN),
    )


@pytest.fixture
def chat(
    bot_settings: BotSettings, redis: Redis, session_factory: async_sessionmaker[AsyncSession]
) -> Chat:
    return Chat(bot_settings, redis, session_factory)
