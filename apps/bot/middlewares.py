"""Update-level protections that run before any handler."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DeduplicateUpdatesMiddleware(BaseMiddleware):
    """Process each Telegram update_id at most once.

    Telegram can redeliver an update (e.g. after a restart before the offset was
    confirmed). The first delivery claims the update_id in Redis; later ones are dropped.

    If Redis is unavailable the update is processed anyway: dropping every update
    would take the bot offline, and operations with side effects (charges, matches)
    carry their own database-level idempotency rather than relying on this layer.
    """

    def __init__(self, redis: Redis, ttl_seconds: int, key_prefix: str = "bot:update:") -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._prefix = key_prefix

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        try:
            first = await self._redis.set(
                f"{self._prefix}{event.update_id}", 1, nx=True, ex=self._ttl
            )
        except RedisError:
            logger.warning("update deduplication unavailable; processing update")
            return await handler(event, data)
        if not first:
            logger.info("duplicate update dropped", extra={"update_id": event.update_id})
            return None
        return await handler(event, data)


class FloodControlMiddleware(BaseMiddleware):
    """Drop updates from a user who sends more than `limit` in a fixed time window.

    Fails open when Redis is unavailable, for the same reason as deduplication.
    """

    def __init__(
        self, redis: Redis, limit: int, window_seconds: int, key_prefix: str = "bot:flood:"
    ) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds
        self._prefix = key_prefix

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        window = int(time.time()) // self._window
        key = f"{self._prefix}{user.id}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window * 2)
                count, _ = await pipe.execute()
        except RedisError:
            logger.warning("flood control unavailable; processing update")
            return await handler(event, data)

        if count > self._limit:
            # The user ID is deliberately not logged; this is routine and frequent.
            logger.info("update dropped by flood control")
            return None
        return await handler(event, data)
