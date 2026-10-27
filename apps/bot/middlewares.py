"""Update-level protections that run before any handler."""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

DONE = "done"

# Claims an update for this process unless it is already done or being processed by
# this same process. A lease held by a different process is taken over: Telegram
# allows only one long-polling consumer per bot, so another holder must have stopped
# (e.g. crashed mid-update), and the redelivery should be processed, not dropped.
_CLAIM_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == false or (current ~= ARGV[1] and current ~= 'done') then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    return 1
end
return 0
"""


class DeduplicateUpdatesMiddleware(BaseMiddleware):
    """Process each Telegram update_id once, without losing updates to failures.

    1. Before handling, the update is claimed with a short lease tagged with this
       process's id. A redelivery while the lease is held by this process is dropped.
    2. After the handler succeeds, the update is marked done for `ttl_seconds`, and
       later redeliveries are dropped.
    3. If the handler raises, the claim is released and the error propagates.

    Telegram only redelivers updates that were never confirmed, i.e. when the process
    stopped mid-update; aiogram confirms an update once received, even if its handler
    fails, so failed updates are not retried (the user gets a generic error instead).
    The lease takeover in `_CLAIM_SCRIPT` makes sure a redelivery after a crash is
    processed rather than dropped.

    If Redis is unavailable the update is processed anyway: dropping every update
    would take the bot offline, and operations with side effects (charges, matches)
    carry their own database-level idempotency rather than relying on this layer.
    """

    def __init__(
        self,
        redis: Redis,
        ttl_seconds: int,
        lease_seconds: int,
        key_prefix: str = "bot:update:",
        instance_id: str | None = None,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._lease = lease_seconds
        self._prefix = key_prefix
        self._lease_value = f"processing:{instance_id or uuid.uuid4().hex}"

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        key = f"{self._prefix}{event.update_id}"
        try:
            claimed = await self._redis.eval(
                _CLAIM_SCRIPT, 1, key, self._lease_value, str(self._lease)
            )
        except RedisError:
            logger.warning("update deduplication unavailable; processing update")
            return await handler(event, data)
        if not claimed:
            logger.info("duplicate update dropped", extra={"update_id": event.update_id})
            return None

        try:
            result = await handler(event, data)
        except BaseException:
            await self._settle(key, done=False)
            raise
        await self._settle(key, done=True)
        return result

    async def _settle(self, key: str, *, done: bool) -> None:
        try:
            if done:
                await self._redis.set(key, DONE, ex=self._ttl)
            else:
                await self._redis.delete(key)
        except RedisError:
            # The lease expires on its own, so a failure here only delays a retry.
            logger.warning("could not settle update deduplication state")


class FloodControlMiddleware(BaseMiddleware):
    """Drop updates from a user who sends more than `limit` in a fixed time window.

    Fails open when Redis is unavailable, for the same reason as deduplication.
    """

    def __init__(
        self,
        redis: Redis,
        limit: int,
        window_seconds: int,
        key_prefix: str = "bot:flood:",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds
        self._prefix = key_prefix
        self._clock = clock

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        window = int(self._clock()) // self._window
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
