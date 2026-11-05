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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.coins import service as coins
from backend.modules.matching.queue import (
    Claimed,
    Search,
    SearchQueue,
    parse_search,
    search_hold_key,
)

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

    async def matched(
        self, conversation_id: int, first: int, second: int, charged: dict[int, int]
    ) -> None:
        """`charged`: the coins each paying user was charged for this match (D-050)."""

    async def search_expired(self, user_id: int, refunded: bool) -> None:
        """`refunded`: whether coins held for the search were given back."""


# Held coins of a paid search still held this long after its time limit are released.
STALE_HOLD_MARGIN = timedelta(minutes=10)


def fits(wants: str | None, gender: str | None) -> bool:
    """Whether someone of `gender` fits a search for `wants` (None: anyone)."""
    return wants is None or wants == gender


def pair_users(
    waiting: Sequence[int],
    recent: set[frozenset[int]],
    can_pair: Callable[[int, int], bool] = lambda first, second: True,
) -> list[tuple[int, int]]:
    """Pair users in waiting order (oldest first), only where `can_pair` allows.

    The first pass never pairs recent partners. The second pass pairs whoever is left,
    so recent partners are matched again only when nobody else is available (D-033).
    `can_pair` is how paid searches limit who they meet (D-050).
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
                    if can_pair(first, second)
                    and (allow_recent or frozenset((first, second)) not in recent)
                ),
                None,
            )
            if partner is None:
                index += 1
                continue
            pairs.append((first, partner))
            unmatched.remove(first)
            unmatched.remove(partner)
    # Back in waiting order (older user first in each pair), so matches are announced
    # to the longest-waiting users first.
    position = {user_id: index for index, user_id in enumerate(waiting)}
    ordered = [
        tuple(sorted(pair, key=position.__getitem__))
        for pair in _swap_out_repeats(pairs, recent, can_pair)
    ]
    return sorted(((a, b) for a, b in ordered), key=lambda pair: position[pair[0]])


def _swap_out_repeats(
    pairs: list[tuple[int, int]],
    recent: set[frozenset[int]],
    can_pair: Callable[[int, int], bool],
) -> list[tuple[int, int]]:
    """Exchange partners between two pairs when that turns a repeat into fresh pairs.

    Greedy pairing can be forced into a repeat that a different arrangement avoids,
    e.g. 1-2 then 3-4 (recent) where 1-3 and 2-4 are both fresh. The same users are
    matched either way; only who meets whom changes. Each swap removes at least one
    repeat and adds none, so this ends.
    """

    def fresh(first: int, second: int) -> bool:
        return frozenset((first, second)) not in recent

    pairs = list(pairs)
    improved = True
    while improved:
        improved = False
        for i, (a, b) in enumerate(pairs):
            if fresh(a, b):
                continue
            for j, (c, d) in enumerate(pairs):
                if i == j:
                    continue
                for new in (((a, c), (b, d)), ((a, d), (b, c))):
                    if not all(can_pair(*pair) for pair in new):
                        continue  # a swap never makes an unwanted match
                    before = fresh(a, b) + fresh(c, d)
                    if sum(fresh(*pair) for pair in new) > before:
                        pairs[i], pairs[j] = new
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
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

        cutoff = now.timestamp() - timeout
        expired = await self._expire(cutoff, now)
        async with self._sessions() as session:
            await coins.release_stale(
                session, "paid_match", now - timedelta(seconds=timeout) - STALE_HOLD_MARGIN, now
            )
            await session.commit()

        # Only searches still within the time limit, even if more were left to expire.
        searches = await self._queue.waiting(BATCH_SIZE, started_after=cutoff)
        waiting = [user_id for user_id, _ in searches]
        choices = await self._queue.searches(waiting)
        async with self._sessions() as session:
            eligible = await chat.eligible_for_matching(session, waiting, self._terms_version)
            ordered = [user_id for user_id in waiting if user_id in eligible and user_id in choices]
            genders = await chat.genders(session, ordered)
            recent = (
                await chat.recent_pairs(session, ordered, now - timedelta(seconds=window))
                if window > 0
                else set()
            )
            await session.commit()

        dropped = [(user_id, started) for user_id, started in searches if user_id not in eligible]
        for user_id, started in dropped:
            if await self._queue.remove(user_id, started) and user_id in choices:
                await self._release_hold(user_id, choices[user_id], now)

        def can_pair(first: int, second: int) -> bool:
            # Each must fit the other's choice: a free search fits anyone (D-050).
            return fits(choices[first].wants, genders.get(second)) and fits(
                choices[second].wants, genders.get(first)
            )

        matched = 0
        for first, second in pair_users(ordered, recent, can_pair):
            if await self._match(first, second, now):
                matched += 1
        return TickResult(matched=matched, expired=expired, dropped=len(dropped))

    async def _expire(self, started_before: float, now: datetime) -> int:
        expired = 0
        candidates = await self._queue.started_before(started_before, BATCH_SIZE)
        choices = await self._queue.searches(candidates)
        for user_id in candidates:
            # The script re-checks the start time, so a search restarted meanwhile stays.
            if not await self._queue.expire(user_id, started_before):
                continue
            expired += 1
            search = choices.get(user_id)
            refunded = search is not None and await self._release_hold(user_id, search, now)
            try:
                await self._notifier.search_expired(user_id, refunded)
            except Exception as exc:
                logger.warning("could not notify an expired search", extra=exception_summary(exc))
        return expired

    async def _release_hold(self, user_id: int, search: Search, now: datetime) -> bool:
        """Give back the coins held for a paid search that ended without a match; returns
        whether they are back, also if the stale-hold sweep returned them already."""
        if search.wants is None:
            return False
        async with self._sessions() as session:
            hold_id = await coins.hold_id_for(session, search_hold_key(user_id, search.token))
            if hold_id is not None:
                await coins.release(session, hold_id, now)
            status = None if hold_id is None else await coins.hold_status(session, hold_id)
            await session.commit()
        return status == "released"

    async def _match(self, first: int, second: int, now: datetime) -> bool:
        claimed = await self._queue.claim_pair(first, second)
        if claimed is None:
            return False  # one of them cancelled or their search ended meanwhile
        try:
            conversation_id, eligible, charged = await self._create(claimed, now)
        except BaseException:
            # A database failure must not lose the searches: put both back, then fail.
            for user in claimed:
                await self._queue.requeue(user)
            raise
        if conversation_id is None:
            # Not matched: whoever can still be matched goes back in their old place.
            for user in claimed:
                if user.user_id in eligible:
                    await self._queue.requeue(user)
                else:
                    await self._release(user)
                    await self._release_hold(user.user_id, parse_search(user.search_id), now)
            return False
        # The match is committed: releasing the searches must not stop the announcement.
        for user in claimed:
            await self._release(user)
        try:
            await self._notifier.matched(conversation_id, first, second, charged)
        except Exception as exc:
            # The match stands; a user who cannot be reached is handled with chat endings.
            logger.warning("could not notify a match", extra=exception_summary(exc))
        return True

    async def _release(self, claimed: Claimed) -> None:
        """Release a claimed search. On a Redis failure the claim marker simply expires."""
        try:
            await self._queue.release(claimed)
        except RedisError as exc:
            logger.warning("could not release a claimed search", extra=exception_summary(exc))

    async def _create(
        self, claimed: tuple[Claimed, Claimed], now: datetime
    ) -> tuple[int | None, set[int], dict[int, int]]:
        """Re-check both users, create their conversation, and charge paid searches, all
        in one transaction (D-050).

        Returns the conversation id (None if not created), who was eligible, and the coins
        charged to each paying user. Both users' rows are share-locked by the check, so
        account deletion, which locks the user row first, either finishes before (and the
        user is not eligible) or waits until this match is committed (and then ends the
        new chat). Coins are taken only here, with the match itself: if their hold can't
        be captured, the match is not made and that user is not put back.
        """
        first, second = claimed[0].user_id, claimed[1].user_id
        async with self._sessions() as session:
            eligible = await chat.eligible_for_matching(
                session, (first, second), self._terms_version, lock=True
            )
            if eligible != {first, second}:
                await session.rollback()
                return None, eligible, {}
            # Choices and genders were read before the claim. Check them again here, from
            # the searches actually claimed and the genders under lock (a gender change
            # locks the profile), so nobody pays for a partner who no longer fits.
            genders = await chat.genders(session, (first, second), lock=True)
            wants = {user.user_id: parse_search(user.search_id).wants for user in claimed}
            if not (
                fits(wants[first], genders.get(second)) and fits(wants[second], genders.get(first))
            ):
                await session.rollback()
                return None, eligible, {}  # both go back in the queue
            try:
                conversation_id = await chat.start_conversation(session, first, second)
            except IntegrityError:
                await session.rollback()
                return None, eligible, {}
            charged: dict[int, int] = {}
            for user in claimed:
                search = parse_search(user.search_id)
                if search.wants is None:
                    continue
                hold_id = await coins.hold_id_for(
                    session, search_hold_key(user.user_id, search.token)
                )
                if hold_id is None or not await coins.capture(session, hold_id, now):
                    await session.rollback()
                    return None, eligible - {user.user_id}, {}
                charged[user.user_id] = await coins.hold_amount(session, hold_id)
            await session.commit()
            return conversation_id, eligible, charged
