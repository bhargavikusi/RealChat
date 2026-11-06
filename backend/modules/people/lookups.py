"""The limit on wrong RealChat ID lookups (D-054), kept in Redis.

Each user's wrong lookups are counted in a window of an hour from the first one. Once
the count reaches the limit (an admin setting), every lookup is refused until the
window ends, even for a correct ID, so IDs cannot be found by trying many in turn.

If Redis is unavailable the limit cannot be checked, and lookups are refused
(LookupsUnavailable) rather than left unlimited.

User ids are internal `users.id` values, never Telegram IDs.
"""

from redis.asyncio import Redis
from redis.exceptions import RedisError

WINDOW_SECONDS = 60 * 60
KEY_PREFIX = "userid:misses:"


class LookupsUnavailable(Exception):
    """The limit could not be checked or counted."""


class IdLookups:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def paused(self, user_id: int, limit: int) -> bool:
        """Whether the user has reached `limit` wrong lookups in the current window."""
        try:
            count = await self._redis.get(f"{KEY_PREFIX}{user_id}")
        except RedisError as exc:
            raise LookupsUnavailable from exc
        return count is not None and int(count) >= limit

    async def miss(self, user_id: int) -> None:
        """Count a wrong lookup; the first one starts the window."""
        key = f"{KEY_PREFIX}{user_id}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(key, 0, ex=WINDOW_SECONDS, nx=True)
                pipe.incr(key)
                await pipe.execute()
        except RedisError as exc:
            raise LookupsUnavailable from exc
