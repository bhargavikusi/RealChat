"""Pairs searching users (D-027, D-032, D-033). Runs in the worker, one tick at a time.

Each tick:
1. Searches older than the time limit are ended, and those users are told.
2. The longest-waiting searchers are re-checked in PostgreSQL. Anyone no longer
   eligible (restricted, deleted, already in a chat, terms not current) is dropped.
3. Eligible users are paired, oldest first, avoiding recent partners while anyone
   else is available.
4. Each pair is taken out of the queue atomically, then the conversation is created
   and committed in PostgreSQL. Only after that commit are both users told. If the
   database refuses the match, both are put back in the queue in their old place.

A Redis lock lets only one matcher run at a time, so pairing never races itself; the
unique index on open conversations still guards against any double match.
"""

import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.matching.queue import SearchQueue

logger = logging.getLogger(__name__)

LOCK_KEY = "match:matcher_lock"
LOCK_TTL_MS = 30_000
BATCH_SIZE = 200

_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class MatchNotifier(Protocol):
    """Tells users about matches and ended searches. Implemented by the bot layer."""

    async def matched(self, conversation_id: int, first: int, second: int) -> None: ...

    async def search_expired(self, user_id: int) -> None: ...


def pair_users(waiting: Sequence[int], recent: set[frozenset[int]]) -> list[tuple[int, int]]:
    """Pair users in waiting order (oldest first).

    The first pass never pairs recent partners. The second pass pairs whoever is left,
    so recent partners are matched again only when nobody else is available (D-033).
    """
    unmatched = list(waiting)
    pairs: list[tuple[int, int]] = []
    for allow_recent in (False, True):
        index = 0
        while index < len(unmatched):
            first = unmatched[index]
            partner = next(
                (
                    second
                    for second in unmatched[index + 1 :]
                    if allow_recent or frozenset((first, second)) not in recent
                ),
                None,
            )
            if partner is None:
                index += 1
                continue
            pairs.append((first, partner))
            unmatched.remove(first)
            unmatched.remove(partner)
    return pairs


@dataclass(frozen=True)
class TickResult:
    matched: int = 0
    expired: int = 0
    dropped: int = 0
    skipped: bool = False  # another matcher held the lock


class Matcher:
    def __init__(
        self,
        redis: Redis,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
        notifier: MatchNotifier,
        terms_version: str,
    ) -> None:
        self._redis = redis
        self._queue = SearchQueue(redis)
        self._sessions = sessions
        self._runtime = runtime_settings
        self._notifier = notifier
        self._terms_version = terms_version

    async def tick(self, now: datetime) -> TickResult:
        token = secrets.token_hex(8)
        if not await self._redis.set(LOCK_KEY, token, nx=True, px=LOCK_TTL_MS):
            return TickResult(skipped=True)
        try:
            return await self._run(now)
        finally:
            await self._redis.eval(_RELEASE_LOCK, 1, LOCK_KEY, token)

    async def _run(self, now: datetime) -> TickResult:
        async with self._sessions() as session:
            timeout = await self._runtime.get(session, SettingKey.MATCH_SEARCH_TIMEOUT_SECONDS)
            window = await self._runtime.get(session, SettingKey.MATCH_REPEAT_WINDOW_SECONDS)
            await session.commit()

        expired = await self._expire(now.timestamp() - timeout)

        waiting = [user_id for user_id, _ in await self._queue.waiting(BATCH_SIZE)]
        async with self._sessions() as session:
            eligible = await chat.eligible_for_matching(session, waiting, self._terms_version)
            ordered = [user_id for user_id in waiting if user_id in eligible]
            recent = (
                await chat.recent_pairs(session, ordered, now - timedelta(seconds=window))
                if window > 0
                else set()
            )
            await session.commit()

        dropped = [user_id for user_id in waiting if user_id not in eligible]
        for user_id in dropped:
            await self._queue.remove(user_id)

        matched = 0
        for first, second in pair_users(ordered, recent):
            if await self._match(first, second):
                matched += 1
        return TickResult(matched=matched, expired=expired, dropped=len(dropped))

    async def _expire(self, started_before: float) -> int:
        expired = 0
        for user_id in await self._queue.started_before(started_before, BATCH_SIZE):
            # The script re-checks the start time, so a search restarted meanwhile stays.
            if not await self._queue.expire(user_id, started_before):
                continue
            expired += 1
            try:
                await self._notifier.search_expired(user_id)
            except Exception as exc:
                logger.warning("could not notify an expired search", extra=exception_summary(exc))
        return expired

    async def _match(self, first: int, second: int) -> bool:
        claimed = await self._queue.claim_pair(first, second)
        if claimed is None:
            return False  # one of them cancelled in the meantime
        async with self._sessions() as session:
            # Re-checked in the transaction that creates the match, so a user restricted
            # or matched since the batch check is not matched now.
            eligible = await chat.eligible_for_matching(
                session, (first, second), self._terms_version
            )
            conversation_id: int | None = None
            if eligible == {first, second}:
                try:
                    conversation_id = await chat.start_conversation(session, first, second)
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    conversation_id = None
        if conversation_id is None:
            # Not matched: the users who can still be matched go back in their old place.
            for user in claimed:
                if user.user_id in eligible:
                    await self._queue.requeue(user)
            return False
        try:
            await self._notifier.matched(conversation_id, first, second)
        except Exception as exc:
            # The match stands; a user who cannot be reached is handled with chat endings.
            logger.warning("could not notify a match", extra=exception_summary(exc))
        return True
