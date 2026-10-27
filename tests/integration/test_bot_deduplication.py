"""Duplicate-update protection must never lose an update to a failure or crash."""

from collections.abc import AsyncIterator

import pytest
from aiogram import Dispatcher, Router
from aiogram.types import Message
from redis.asyncio import Redis

from apps.bot import errors
from apps.bot.middlewares import DONE, DeduplicateUpdatesMiddleware
from backend.core.redis import create_redis
from tests.support.telegram import make_bot, message_update

PREFIX = "bot:update:"


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    # TEST_REDIS_URL points at a database used only by tests, so flushing it is safe.
    client = create_redis(redis_url)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


class FlakyHandler:
    """Fails on the first call, succeeds afterwards.

    aiogram only awaits real coroutine functions, so `handle` is registered rather
    than the instance itself.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, message: Message) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient failure")
        await message.answer("ok")


def _dispatcher(redis: Redis, handler: FlakyHandler, instance_id: str) -> Dispatcher:
    router = Router(name="flaky")
    router.message.register(handler.handle)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(
        DeduplicateUpdatesMiddleware(
            redis, ttl_seconds=600, lease_seconds=60, instance_id=instance_id
        )
    )
    dispatcher.include_routers(errors.create_router(), router)
    return dispatcher


async def test_failed_update_is_released_for_redelivery(redis: Redis) -> None:
    bot, session = make_bot()
    handler = FlakyHandler()
    dispatcher = _dispatcher(redis, handler, "instance-a")
    update = message_update(1, 7, "hello")

    await dispatcher.feed_update(bot, update)  # fails, user gets the generic error
    await dispatcher.feed_update(bot, update)  # redelivery is processed

    assert handler.calls == 2
    assert session.sent_texts() == [errors.GENERIC_ERROR_TEXT, "ok"]
    assert await redis.get(f"{PREFIX}1") == DONE


async def test_successful_update_is_not_processed_again(redis: Redis) -> None:
    bot, _ = make_bot()
    handler = FlakyHandler()
    handler.calls = 1  # skip the failing first call
    dispatcher = _dispatcher(redis, handler, "instance-a")
    update = message_update(2, 7, "hello")

    await dispatcher.feed_update(bot, update)
    await dispatcher.feed_update(bot, update)

    assert handler.calls == 2  # one successful call after the preset
    assert await redis.get(f"{PREFIX}2") == DONE


async def test_lease_left_by_crashed_process_is_taken_over(redis: Redis) -> None:
    # A previous bot process claimed the update and died before settling it.
    await redis.set(f"{PREFIX}3", "processing:crashed-instance", ex=60)
    bot, session = make_bot()
    handler = FlakyHandler()
    handler.calls = 1
    dispatcher = _dispatcher(redis, handler, "instance-b")

    await dispatcher.feed_update(bot, message_update(3, 7, "hello"))

    assert session.sent_texts() == ["ok"]


async def test_duplicate_during_own_processing_is_dropped(redis: Redis) -> None:
    # This same process is still handling the update when a duplicate arrives.
    await redis.set(f"{PREFIX}4", "processing:instance-a", ex=60)
    bot, session = make_bot()
    handler = FlakyHandler()
    dispatcher = _dispatcher(redis, handler, "instance-a")

    await dispatcher.feed_update(bot, message_update(4, 7, "hello"))

    assert handler.calls == 0
    assert session.sent_texts() == []
