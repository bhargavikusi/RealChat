"""The search queue in Redis (D-027).

Who is searching is temporary state: a sorted set of user ids scored by when their
search started, plus one key per user holding the state of their current search:

* `<id>`: searching. The id is carried by the Cancel button, so a button from an
  older search cannot cancel a newer one.
* `claimed:<id>`: taken out of the queue by the matcher, which is creating the match.
* `cancelled`: the user cancelled while the matcher held the search. If the match then
  fails, the search is not put back, so a user told their search ended is never matched
  later from it.

Every operation that touches both runs as one Lua script, so it is atomic even against
the matcher.

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
# A claim normally lasts milliseconds. Its marker expires on its own, so one left
# behind (e.g. if Redis failed while releasing it) cannot block new searches for long.
CLAIM_TTL_SECONDS = 60
CLAIMED = "claimed:"
CANCELLED = "cancelled"

# A search held by the matcher is reported as still running under its own id; a
# cancelled one is replaced by a new search.
_START = """
local current = redis.call('GET', KEYS[1])
if current and current ~= 'cancelled' then
    if string.sub(current, 1, 8) == 'claimed:' then
        return {0, string.sub(current, 9)}
    end
    redis.call('ZADD', KEYS[2], 'NX', ARGV[2], ARGV[4])
    return {0, current}
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[4])
return {1, ARGV[1]}
"""

_CANCEL = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('ZREM', KEYS[2], ARGV[2])
    return 1
end
if current == 'claimed:' .. ARGV[1] then
    redis.call('SET', KEYS[1], 'cancelled', 'KEEPTTL')
end
return 0
"""

# Claims both users only if both are queued with a live search. A queue entry whose
# search key is gone (it expired, e.g. after a long worker outage) is an orphan and is
# removed instead of being matched.
_CLAIM_PAIR = """
local function live(key)
    local value = redis.call('GET', key)
    if not value or value == 'cancelled' or string.sub(value, 1, 8) == 'claimed:' then
        return nil
    end
    return value
end
local first = redis.call('ZSCORE', KEYS[3], ARGV[1])
local second = redis.call('ZSCORE', KEYS[3], ARGV[2])
local first_search = live(KEYS[1])
local second_search = live(KEYS[2])
if first and not first_search then redis.call('ZREM', KEYS[3], ARGV[1]) end
if second and not second_search then redis.call('ZREM', KEYS[3], ARGV[2]) end
if not (first and second and first_search and second_search) then
    return {}
end
redis.call('ZREM', KEYS[3], ARGV[1], ARGV[2])
redis.call('SET', KEYS[1], 'claimed:' .. first_search, 'EX', ARGV[3])
redis.call('SET', KEYS[2], 'claimed:' .. second_search, 'EX', ARGV[3])
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

# Removes only the search that was observed (same start time), not a newer one.
_REMOVE = """
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
if score and tonumber(score) == tonumber(ARGV[2]) then
    redis.call('ZREM', KEYS[2], ARGV[1])
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""

# Puts a claimed search back in its old place, unless it was cancelled meanwhile.
_REQUEUE = """
if redis.call('GET', KEYS[1]) == 'claimed:' .. ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
    redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
    return 1
end
return 0
"""

# Ends a claimed search once the matcher is done with it (matched, or not eligible).
_RELEASE = """
local current = redis.call('GET', KEYS[1])
if current == 'claimed:' .. ARGV[1] or current == 'cancelled' then
    redis.call('DEL', KEYS[1])
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
        """Cancel the search with this id. False if it already ended or is being matched.

        A search the matcher holds is marked cancelled, so it is not put back if that
        match fails.
        """
        cancelled = await self._eval(
            _CANCEL, 2, _search_key(user_id), QUEUE_KEY, search_id, user_id
        )
        return bool(cancelled)

    async def waiting(self, limit: int, started_after: float) -> list[tuple[int, float]]:
        """(user id, start time) of the longest-waiting searches newer than `started_after`."""
        if limit <= 0:
            return []
        rows = cast(
            list[tuple[str, float]],
            await self._redis.zrangebyscore(
                QUEUE_KEY, f"({started_after}", "+inf", start=0, num=limit, withscores=True
            ),
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
            _CLAIM_PAIR,
            3,
            _search_key(first),
            _search_key(second),
            QUEUE_KEY,
            first,
            second,
            CLAIM_TTL_SECONDS,
        )
        if not result:
            return None
        first_score, first_search, second_score, second_search = result
        return (
            Claimed(first, float(first_score), str(first_search)),
            Claimed(second, float(second_score), str(second_search)),
        )

    async def requeue(self, claimed: Claimed) -> bool:
        """Put a claimed search back in its old place, unless it was cancelled meanwhile."""
        restored = await self._eval(
            _REQUEUE,
            2,
            _search_key(claimed.user_id),
            QUEUE_KEY,
            claimed.search_id,
            claimed.started_at,
            claimed.user_id,
            SEARCH_TTL_SECONDS,
        )
        return bool(restored)

    async def release(self, claimed: Claimed) -> None:
        """End a claimed search that is not going back in the queue."""
        await self._eval(_RELEASE, 1, _search_key(claimed.user_id), claimed.search_id)

    async def expire(self, user_id: int, started_before: float) -> bool:
        """End the user's search if it started at or before `started_before`."""
        expired = await self._eval(
            _EXPIRE, 2, _search_key(user_id), QUEUE_KEY, user_id, started_before
        )
        return bool(expired)

    async def remove(self, user_id: int, started_at: float) -> bool:
        """Drop the search that started at `started_at`, e.g. of a user no longer eligible.

        A newer search by the same user is left alone.
        """
        removed = await self._eval(_REMOVE, 2, _search_key(user_id), QUEUE_KEY, user_id, started_at)
        return bool(removed)
