"""The search queue in Redis (D-027).

Who is searching is temporary state: a sorted set of user ids scored by when their
search started, plus one key per user holding the id of their current search. The
search id is carried by the "Cancel" button, so a button from an older search cannot
cancel a newer one. Each operation that touches both runs as one Lua script, so it is
atomic even against the matcher.

Losing Redis loses the queue, never a match: matches are confirmed in PostgreSQL, and
every user taken from the queue is re-checked there first. After a Redis restart,
users who were searching are simply no longer searching and can start again.

User ids are internal `users.id` values, never Telegram IDs.
"""

import secrets
from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis

QUEUE_KEY = "match:queue"
# A safety net only: the matcher ends searches after the configured time limit. If
# the worker is down, abandoned search keys still disappear eventually.
SEARCH_TTL_SECONDS = 2 * 60 * 60

_START = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[3]) then
    redis.call('ZADD', KEYS[2], ARGV[2], ARGV[4])
    return {1, ARGV[1]}
end
redis.call('ZADD', KEYS[2], 'NX', ARGV[2], ARGV[4])
return {0, redis.call('GET', KEYS[1])}
"""

_CANCEL = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('ZREM', KEYS[2], ARGV[2])
    return 1
end
return 0
"""

_CLAIM_PAIR = """
local first = redis.call('ZSCORE', KEYS[3], ARGV[1])
local second = redis.call('ZSCORE', KEYS[3], ARGV[2])
if not first or not second then
    return {}
end
local first_search = redis.call('GET', KEYS[1]) or ''
local second_search = redis.call('GET', KEYS[2]) or ''
redis.call('ZREM', KEYS[3], ARGV[1], ARGV[2])
redis.call('DEL', KEYS[1], KEYS[2])
return {first, first_search, second, second_search}
"""

_EXPIRE = """
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
if score and tonumber(score) <= tonumber(ARGV[2]) then
    redis.call('ZREM', KEYS[2], ARGV[1])
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""

_REQUEUE = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[3]) then
    redis.call('ZADD', KEYS[2], ARGV[2], ARGV[4])
    return 1
end
return 0
"""


def _search_key(user_id: int) -> str:
    return f"match:search:{user_id}"


@dataclass(frozen=True)
class Claimed:
    """A user taken from the queue by the matcher, with what is needed to put them back."""

    user_id: int
    started_at: float
    search_id: str


class SearchQueue:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def _eval(self, script: str, numkeys: int, *args: str | int | float) -> Any:
        # Script results are lists or integers; their shape is checked by each caller.
        return await self._redis.eval(script, numkeys, *args)

    async def start(self, user_id: int, now: float) -> tuple[str, bool]:
        """Start searching; returns (search id, True if new). A repeat returns the current id."""
        created, search_id = await self._eval(
            _START,
            2,
            _search_key(user_id),
            QUEUE_KEY,
            secrets.token_hex(4),
            now,
            SEARCH_TTL_SECONDS,
            user_id,
        )
        return str(search_id), bool(created)

    async def current(self, user_id: int) -> str | None:
        value = await self._redis.get(_search_key(user_id))
        return None if value is None else str(value)

    async def cancel(self, user_id: int, search_id: str) -> bool:
        """Cancel the search with this id. False if it already ended (matched or expired)."""
        cancelled = await self._eval(
            _CANCEL, 2, _search_key(user_id), QUEUE_KEY, search_id, user_id
        )
        return bool(cancelled)

    async def waiting(self, limit: int) -> list[tuple[int, float]]:
        """(user id, start time) of the longest-waiting searchers, oldest first."""
        rows = cast(
            list[tuple[str, float]],
            await self._redis.zrange(QUEUE_KEY, 0, limit - 1, withscores=True),
        )
        return [(int(member), float(score)) for member, score in rows]

    async def started_before(self, cutoff: float, limit: int) -> list[int]:
        """Users whose search started at or before `cutoff`, oldest first."""
        members = cast(
            list[str],
            await self._redis.zrangebyscore(QUEUE_KEY, "-inf", cutoff, start=0, num=limit),
        )
        return [int(member) for member in members]

    async def claim_pair(self, first: int, second: int) -> tuple[Claimed, Claimed] | None:
        """Take both users out of the queue, or neither if either has left it."""
        result = await self._eval(
            _CLAIM_PAIR, 3, _search_key(first), _search_key(second), QUEUE_KEY, first, second
        )
        if not result:
            return None
        first_score, first_search, second_score, second_search = result
        return (
            Claimed(first, float(first_score), str(first_search)),
            Claimed(second, float(second_score), str(second_search)),
        )

    async def requeue(self, claimed: Claimed) -> bool:
        """Put a claimed user back with their original place, unless they started anew."""
        search_id = claimed.search_id or secrets.token_hex(4)
        restored = await self._eval(
            _REQUEUE,
            2,
            _search_key(claimed.user_id),
            QUEUE_KEY,
            search_id,
            claimed.started_at,
            SEARCH_TTL_SECONDS,
            claimed.user_id,
        )
        return bool(restored)

    async def expire(self, user_id: int, started_before: float) -> bool:
        """End the user's search if it started at or before `started_before`."""
        expired = await self._eval(
            _EXPIRE, 2, _search_key(user_id), QUEUE_KEY, user_id, started_before
        )
        return bool(expired)

    async def remove(self, user_id: int) -> None:
        """Drop a user from the queue, e.g. one who is no longer eligible."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zrem(QUEUE_KEY, user_id)
            pipe.delete(_search_key(user_id))
            await pipe.execute()
