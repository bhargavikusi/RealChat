from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from apps.bot.main import create_dispatcher
from apps.bot.start import PLACEHOLDER_TEXT
from backend.config.settings import BotSettings
from backend.core.redis import create_redis
from tests.support.telegram import FAKE_TOKEN, make_bot, message_update

FLOOD_LIMIT = 3
# A frozen clock keeps every flood-test update inside one window.
FROZEN_NOW = 1_000_000.0


def _settings(database_url: str, redis_url: str) -> BotSettings:
    return BotSettings(
        _env_file=None,
        app_env="test",
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
        telegram_bot_token=SecretStr(FAKE_TOKEN),
        bot_flood_limit=FLOOD_LIMIT,
        bot_flood_window_seconds=60,
    )


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
    return _settings(database_url, redis_url)


async def test_start_replies_with_placeholder(bot_settings: BotSettings, redis: Redis) -> None:
    bot, session = make_bot()

    await create_dispatcher(bot_settings, redis).feed_update(bot, message_update(1, 7, "/start"))

    assert session.sent_texts() == [PLACEHOLDER_TEXT]


async def test_duplicate_update_is_processed_once(bot_settings: BotSettings, redis: Redis) -> None:
    bot, session = make_bot()
    dispatcher = create_dispatcher(bot_settings, redis)
    update = message_update(10, 7, "/start")

    await dispatcher.feed_update(bot, update)
    await dispatcher.feed_update(bot, update)

    assert session.sent_texts() == [PLACEHOLDER_TEXT]


async def test_flood_limit_drops_excess_updates(bot_settings: BotSettings, redis: Redis) -> None:
    bot, session = make_bot()
    dispatcher = create_dispatcher(bot_settings, redis, clock=lambda: FROZEN_NOW)

    for update_id in range(100, 100 + FLOOD_LIMIT + 2):
        await dispatcher.feed_update(bot, message_update(update_id, 7, "/start"))

    assert len(session.sent_texts()) == FLOOD_LIMIT


async def test_flood_limit_is_per_user(bot_settings: BotSettings, redis: Redis) -> None:
    bot, session = make_bot()
    dispatcher = create_dispatcher(bot_settings, redis, clock=lambda: FROZEN_NOW)

    for update_id in range(200, 200 + FLOOD_LIMIT + 2):
        await dispatcher.feed_update(bot, message_update(update_id, 7, "/start"))
    await dispatcher.feed_update(bot, message_update(300, 8, "/start"))

    assert len(session.sent_texts()) == FLOOD_LIMIT + 1


async def test_bot_keeps_working_when_redis_is_down(database_url: str) -> None:
    # Nothing listens on port 1: both protections fail open rather than drop updates.
    settings = _settings(database_url, "redis://127.0.0.1:1/0")
    unreachable = create_redis(settings.redis_url.get_secret_value())
    bot, session = make_bot()

    await create_dispatcher(settings, unreachable).feed_update(bot, message_update(1, 7, "/start"))
    await unreachable.aclose()

    assert session.sent_texts() == [PLACEHOLDER_TEXT]
