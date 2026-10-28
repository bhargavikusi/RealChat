"""Search queue, matcher, and conversations against real Redis and PostgreSQL (D-027 to D-033)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin import service as admin
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.chat.models import Conversation, ConversationParticipant
from backend.modules.matching import matcher as matcher_module
from backend.modules.matching.matcher import Matcher
from backend.modules.matching.queue import CLAIM_TTL_SECONDS, SearchQueue
from backend.modules.users import service as users
from backend.modules.users.models import User
from backend.modules.users.terms import TERMS_VERSION
from tests.support.db import wait_until_blocked

Sessions = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class RecordingNotifier:
    def __init__(self) -> None:
        self.matches: list[tuple[int, int]] = []
        self.expired: list[int] = []

    async def matched(self, conversation_id: int, first: int, second: int) -> None:
        self.matches.append((first, second))

    async def search_expired(self, user_id: int) -> None:
        self.expired.append(user_id)


async def _eligible_user(sessions: Sessions, telegram_id: int) -> int:
    """A fully onboarded user who accepted the current terms."""
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        profile = await users.get_profile(session, user.id)
        users.set_nickname(profile, f"User {telegram_id}")
        users.set_age(profile, 25, minimum_age=18, today=NOW.date())
        users.set_gender(profile, "other")
        users.set_country(profile, "IN")
        profile.onboarding_completed_at = NOW
        await users.accept_terms(session, user.id, TERMS_VERSION)
        await session.commit()
        return user.id


@pytest.fixture
async def people(session_factory: Sessions) -> list[int]:
    return [await _eligible_user(session_factory, 1000 + n) for n in range(4)]


@pytest.fixture
def queue(redis: Redis) -> SearchQueue:
    return SearchQueue(redis)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def matcher(redis: Redis, session_factory: Sessions, notifier: RecordingNotifier) -> Matcher:
    # No settings cache, so a changed setting applies on the next tick.
    return Matcher(redis, session_factory, RuntimeSettings(ttl_seconds=0), notifier, TERMS_VERSION)


async def _search(queue: SearchQueue, *user_ids: int, at: datetime = NOW) -> None:
    for offset, user_id in enumerate(user_ids):
        await queue.start(user_id, at.timestamp() + offset)  # keeps the given order


async def _open_pairs(sessions: Sessions) -> set[frozenset[int]]:
    async with sessions() as session:
        result = set()
        for user_id in (
            await session.execute(
                select(ConversationParticipant.user_id).where(
                    ConversationParticipant.left_at.is_(None)
                )
            )
        ).scalars():
            active = await chat.active_chat(session, user_id)
            assert active is not None
            result.add(frozenset((user_id, active.partner_id)))
        return result


async def _end(sessions: Sessions, user_id: int, at: datetime) -> None:
    async with sessions() as session:
        assert await chat.end_conversation(session, user_id, at) is not None
        await session.commit()


async def _set_window(sessions: Sessions, actor: int, seconds: int) -> None:
    async with sessions() as session:
        await session.execute(update(User).where(User.id == actor).values(role="admin"))
        await admin.change_setting(session, actor, SettingKey.MATCH_REPEAT_WINDOW_SECONDS, seconds)
        await session.commit()


# --- Queue ---------------------------------------------------------------------------


async def test_a_repeated_start_keeps_one_search(queue: SearchQueue) -> None:
    first_id, created = await queue.start(1, 100.0)
    second_id, again = await queue.start(1, 200.0)
    assert (created, again, second_id) == (True, False, first_id)
    assert await queue.waiting(10, started_after=0) == [(1, 100.0)]


async def test_only_the_current_search_can_be_cancelled(queue: SearchQueue) -> None:
    search_id, _ = await queue.start(1, 100.0)
    assert not await queue.cancel(1, "older-search")
    assert await queue.cancel(1, search_id)
    assert not await queue.cancel(1, search_id)
    assert await queue.waiting(10, started_after=0) == []


async def test_a_pair_is_claimed_whole_or_not_at_all(queue: SearchQueue) -> None:
    await _search(queue, 1, 2)
    await queue.cancel(2, str(await queue.current(2)))
    assert await queue.claim_pair(1, 2) is None
    assert await queue.waiting(10, started_after=0) == [(1, NOW.timestamp())]


async def test_requeue_restores_the_old_place(queue: SearchQueue) -> None:
    await _search(queue, 1, 2, 3)
    claimed = await queue.claim_pair(1, 2)
    assert claimed is not None
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [3]
    for user in claimed:
        assert await queue.requeue(user)
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [1, 2, 3]
    # The restored searches can still be cancelled with their buttons.
    assert await queue.cancel(1, claimed[0].search_id)


# --- Matching ------------------------------------------------------------------------


async def test_two_searchers_are_matched_and_told(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)
    result = await matcher.tick(NOW)
    assert result.matched == 1
    assert notifier.matches == [(a, b)]
    assert await _open_pairs(session_factory) == {frozenset((a, b))}
    assert await queue.waiting(10, started_after=0) == []


async def test_a_lone_searcher_waits(
    matcher: Matcher, queue: SearchQueue, notifier: RecordingNotifier, people: list[int]
) -> None:
    await _search(queue, people[0])
    assert (await matcher.tick(NOW)).matched == 0
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [people[0]]


async def test_a_cancelled_search_is_not_matched(
    matcher: Matcher, queue: SearchQueue, notifier: RecordingNotifier, people: list[int]
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)
    assert await queue.cancel(b, str(await queue.current(b)))
    await matcher.tick(NOW)
    assert notifier.matches == []


async def test_searches_end_after_the_time_limit(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b = people[:2]
    await _search(queue, a, at=NOW - timedelta(seconds=121))
    await _search(queue, b, at=NOW - timedelta(seconds=119))
    await matcher.tick(NOW)
    # a's search ran out before b's could pair with it; b keeps waiting.
    assert notifier.expired == [a]
    assert notifier.matches == []
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [b]

    async with session_factory() as session:
        await session.execute(update(User).where(User.id == b).values(role="admin"))
        await admin.change_setting(session, b, SettingKey.MATCH_SEARCH_TIMEOUT_SECONDS, 60)
        await session.commit()
    await matcher.tick(NOW)
    assert notifier.expired == [a, b]


@pytest.mark.parametrize(
    "make_ineligible",
    [
        "UPDATE users SET status = 'suspended' WHERE id = :id",
        "UPDATE users SET status = 'banned' WHERE id = :id",
        "UPDATE users SET status = 'deleted', deleted_at = now() WHERE id = :id",
        "UPDATE profiles SET onboarding_completed_at = NULL WHERE user_id = :id",
        "DELETE FROM terms_acceptances WHERE user_id = :id",
    ],
)
async def test_ineligible_users_are_dropped_not_matched(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
    make_ineligible: str,
) -> None:
    a, b = people[:2]
    async with session_factory() as session:
        await session.execute(text(make_ineligible), {"id": b})
        await session.commit()
    await _search(queue, a, b)
    result = await matcher.tick(NOW)
    assert (result.matched, result.dropped) == (0, 1)
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [a]


async def test_an_ineligible_user_does_not_hold_up_the_others(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b, c = people[:3]
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == b).values(status="banned"))
        await session.commit()
    await _search(queue, a, b, c)
    await matcher.tick(NOW)
    assert notifier.matches == [(a, c)]


async def test_users_already_in_a_chat_are_not_matched_again(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b, c = people[:3]
    async with session_factory() as session:
        await chat.start_conversation(session, a, b)
        await session.commit()
    # A stale queue entry for a, e.g. from before the match.
    await _search(queue, a, c)
    await matcher.tick(NOW)
    assert notifier.matches == []
    assert await _open_pairs(session_factory) == {frozenset((a, b))}


async def test_recent_partners_are_avoided_while_others_wait(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b, c = people[:3]
    async with session_factory() as session:
        await chat.start_conversation(session, a, b)
        await session.commit()
    await _end(session_factory, a, NOW - timedelta(seconds=60))
    await _search(queue, a, b, c)
    await matcher.tick(NOW)
    assert notifier.matches == [(a, c)]


async def test_recent_partners_are_matched_when_alone(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b = people[:2]
    async with session_factory() as session:
        await chat.start_conversation(session, a, b)
        await session.commit()
    await _end(session_factory, a, NOW - timedelta(seconds=60))
    await _search(queue, a, b)
    await matcher.tick(NOW)
    assert notifier.matches == [(a, b)]


async def test_repeat_avoidance_can_be_turned_off_and_expires(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b, c, d = people
    for pair in ((a, b), (c, d)):
        async with session_factory() as session:
            await chat.start_conversation(session, *pair)
            await session.commit()
    await _end(session_factory, a, NOW - timedelta(seconds=60))
    await _end(session_factory, c, NOW - timedelta(seconds=301))  # outside the 5 minutes

    await _search(queue, c, d)
    await matcher.tick(NOW)
    assert notifier.matches == [(c, d)]  # the window has passed

    await _set_window(session_factory, d, 0)
    await _search(queue, a, b)
    await _end(session_factory, c, NOW)
    await _search(queue, c, at=NOW + timedelta(seconds=10))  # waits behind a and b
    await matcher.tick(NOW)
    assert notifier.matches[1] == (a, b)  # avoidance is off


async def test_a_refused_match_puts_both_back_in_their_place(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)

    async def refuse(session: AsyncSession, first: int, second: int) -> int:
        raise IntegrityError("insert", {}, Exception("simulated unique violation"))

    monkeypatch.setattr(chat, "start_conversation", refuse)
    await matcher.tick(NOW)
    assert notifier.matches == []
    assert await queue.waiting(10, started_after=0) == [
        (a, NOW.timestamp()),
        (b, NOW.timestamp() + 1),
    ]


async def test_lost_redis_data_creates_no_match(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    redis: Redis,
    session_factory: Sessions,
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)
    await redis.flushdb()  # as after a Redis restart without persistence
    result = await matcher.tick(NOW)
    assert (result.matched, notifier.matches) == (0, [])
    assert await _open_pairs(session_factory) == set()
    # Users simply search again.
    await _search(queue, a, b)
    assert (await matcher.tick(NOW)).matched == 1


# --- Concurrency ---------------------------------------------------------------------


async def test_only_one_matcher_runs_at_a_time(
    matcher: Matcher,
    redis: Redis,
    session_factory: Sessions,
    people: list[int],
    queue: SearchQueue,
) -> None:
    other = Matcher(
        redis, session_factory, RuntimeSettings(ttl_seconds=0), RecordingNotifier(), TERMS_VERSION
    )
    await _search(queue, *people)
    results = await asyncio.gather(matcher.tick(NOW), other.tick(NOW))
    assert sorted(r.skipped for r in results) == [False, True]
    assert sum(r.matched for r in results) == 2
    assert len(await _open_pairs(session_factory)) == 2


async def test_the_database_refuses_a_second_open_conversation(
    session_factory: Sessions, people: list[int]
) -> None:
    a, b, c = people[:3]
    async with session_factory() as session:
        await chat.start_conversation(session, a, b)
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await chat.start_conversation(session, a, c)


async def test_concurrent_matches_for_one_user_create_one_conversation(
    session_factory: Sessions, people: list[int]
) -> None:
    a, b, c = people[:3]

    async def match(partner: int) -> bool:
        async with session_factory() as session:
            try:
                await chat.start_conversation(session, a, partner)
                await session.commit()
                return True
            except IntegrityError:
                return False

    assert sorted(await asyncio.gather(match(b), match(c))) == [False, True]
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Conversation))
    assert count == 1


async def test_simultaneous_ends_end_the_chat_once(
    session_factory: Sessions, people: list[int]
) -> None:
    a, b = people[:2]
    async with session_factory() as session:
        await chat.start_conversation(session, a, b)
        await session.commit()

    async with session_factory() as first:
        assert await chat.end_conversation(first, a, NOW) is not None  # not committed yet

        async def second() -> bool:
            async with session_factory() as session:
                ended = await chat.end_conversation(session, b, NOW)
                await session.commit()
                return ended is not None

        task = asyncio.create_task(second())
        await wait_until_blocked(session_factory)
        assert not task.done()  # waits for the first transaction's lock
        await first.commit()
        assert await task is False  # it sees the chat already ended

    assert await _open_pairs(session_factory) == set()


async def test_users_cannot_be_matched_with_themselves(
    session_factory: Sessions, people: list[int]
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError, match="themselves"):
            await chat.start_conversation(session, people[0], people[0])


# --- Review follow-ups ---------------------------------------------------------------


async def test_searches_beyond_the_expiry_batch_are_never_matched(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(matcher_module, "BATCH_SIZE", 2)
    await _search(queue, *people, at=NOW - timedelta(seconds=600))
    await matcher.tick(NOW)
    assert (notifier.expired, notifier.matches) == (people[:2], [])
    await matcher.tick(NOW)
    assert (notifier.expired, notifier.matches) == (people, [])


async def test_waiting_with_no_room_returns_no_one(queue: SearchQueue) -> None:
    await _search(queue, 1)
    assert await queue.waiting(0, started_after=0) == []


async def test_dropping_a_search_leaves_a_newer_one(queue: SearchQueue) -> None:
    await queue.start(1, 100.0)
    assert not await queue.remove(1, 99.0)  # not the search that was looked at
    assert await queue.remove(1, 100.0)
    assert await queue.waiting(10, started_after=0) == []


async def test_a_cancel_while_claimed_keeps_the_search_out(queue: SearchQueue) -> None:
    await _search(queue, 1, 2)
    claimed = await queue.claim_pair(1, 2)
    assert claimed is not None
    first, second = claimed
    # Tapping 🎭 meanwhile reports the same search, and Cancel cannot cancel it yet.
    assert await queue.start(1, NOW.timestamp() + 5) == (first.search_id, False)
    assert not await queue.cancel(1, first.search_id)
    # The match fails: the cancelled search is not put back, the other one is.
    assert not await queue.requeue(first)
    assert await queue.requeue(second)
    await queue.release(first)
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [2]
    # A new search starts normally afterwards.
    assert (await queue.start(1, NOW.timestamp() + 9))[1] is True


async def test_claimed_searches_end_once_matched(
    matcher: Matcher, queue: SearchQueue, people: list[int]
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)
    await matcher.tick(NOW)
    assert (await queue.current(a), await queue.current(b)) == (None, None)


async def test_orphaned_queue_entries_are_not_claimed(queue: SearchQueue, redis: Redis) -> None:
    await _search(queue, 1, 2)
    await redis.delete("match:search:2")  # its key expired, e.g. during a worker outage
    assert await queue.claim_pair(1, 2) is None
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [1]


async def test_a_database_failure_puts_both_searches_back(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)
    real = chat.eligible_for_matching

    async def failing(*args: object, lock: bool = False, **kwargs: object) -> set[int]:
        if lock:
            raise ConnectionError("simulated database outage")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(chat, "eligible_for_matching", failing)
    with pytest.raises(ConnectionError):
        await matcher.tick(NOW)
    assert notifier.matches == []
    assert await queue.waiting(10, started_after=0) == [
        (a, NOW.timestamp()),
        (b, NOW.timestamp() + 1),
    ]


async def test_a_deletion_in_progress_is_never_matched(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    session_factory: Sessions,
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)
    async with session_factory() as deleting:
        user = await users.lock_user(deleting, a)  # as account deletion does first
        assert await chat.end_conversation(deleting, a, NOW) is None
        task = asyncio.create_task(matcher.tick(NOW))
        await wait_until_blocked(session_factory)  # the match waits for the deletion
        await users.delete_account(deleting, user.id, NOW)
        await deleting.commit()
        await task

    assert notifier.matches == []
    assert await _open_pairs(session_factory) == set()
    assert [user for user, _ in await queue.waiting(10, started_after=0)] == [b]


async def test_a_failed_release_still_announces_the_match(
    matcher: Matcher,
    queue: SearchQueue,
    notifier: RecordingNotifier,
    people: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = people[:2]
    await _search(queue, a, b)

    async def failing_release(self: SearchQueue, claimed: object) -> None:
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(SearchQueue, "release", failing_release)
    assert (await matcher.tick(NOW)).matched == 1
    assert notifier.matches == [(a, b)]


async def test_claim_markers_expire_quickly_and_restored_searches_do_not(
    queue: SearchQueue, redis: Redis
) -> None:
    await _search(queue, 1, 2)
    claimed = await queue.claim_pair(1, 2)
    assert claimed is not None
    assert 0 < await redis.ttl("match:search:1") <= CLAIM_TTL_SECONDS
    assert await queue.requeue(claimed[0])
    assert await redis.ttl("match:search:1") > CLAIM_TTL_SECONDS
