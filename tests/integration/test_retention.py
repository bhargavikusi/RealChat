"""Deleting stored chat messages after the retention period (D-028, D-046)."""

import logging
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import retention as rt
from backend.modules.chat import service as chat
from backend.modules.chat.models import ChatMessage, Conversation, ConversationParticipant
from backend.modules.chat.retention import RUN_KEY, ChatRetention
from backend.modules.moderation.models import Report, UserBlock
from backend.modules.users import service as users

Sessions = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
_message_ids = count(1)


@pytest.fixture
def retention(redis: Redis, session_factory: Sessions) -> ChatRetention:
    return ChatRetention(redis, session_factory, RuntimeSettings(ttl_seconds=0))


async def _people(sessions: Sessions) -> tuple[int, int]:
    async with sessions() as session:
        alice = await users.get_or_create_user(session, 4242)
        bob = await users.get_or_create_user(session, 5353)
        await session.commit()
        return alice.id, bob.id


async def _conversation(sessions: Sessions, first: int, second: int) -> int:
    """A conversation between the two, ended so that a new one can be started."""
    async with sessions() as session:
        conversation_id = await chat.start_conversation(session, first, second)
        await chat.end_conversation(session, first, NOW)
        await session.commit()
    return conversation_id


async def _message(sessions: Sessions, conversation_id: int, sender: int, age: timedelta) -> int:
    async with sessions() as session:
        message = ChatMessage(
            conversation_id=conversation_id,
            sender_user_id=sender,
            sender_message_id=next(_message_ids),
            recipient_message_id=next(_message_ids),
            kind="text",
            text="private words",
            created_at=NOW - age,
        )
        session.add(message)
        await session.commit()
        return message.id


async def _report(
    sessions: Sessions, conversation_id: int, by: int, about: int, *, closed: bool
) -> None:
    async with sessions() as session:
        session.add(
            Report(
                reporter_user_id=by,
                reported_user_id=about,
                conversation_id=conversation_id,
                reason="spam",
                urgent=False,
                status="closed" if closed else "open",
                closed_at=NOW if closed else None,
            )
        )
        await session.commit()


async def _remaining(sessions: Sessions) -> set[int]:
    async with sessions() as session:
        return set((await session.execute(select(ChatMessage.id))).scalars())


async def _count(sessions: Sessions, model: type) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _set_days(sessions: Sessions, days: int) -> None:
    async with sessions() as session:
        await session.merge(AppSetting(key=SettingKey.CHAT_RETENTION_DAYS.value, value=days))
        await session.commit()


DAY = timedelta(days=1)


async def test_expired_messages_are_deleted_and_the_rest_kept(
    retention: ChatRetention, session_factory: Sessions
) -> None:
    alice, bob = await _people(session_factory)
    plain = await _conversation(session_factory, alice, bob)
    reported = await _conversation(session_factory, alice, bob)
    closed = await _conversation(session_factory, alice, bob)
    old = await _message(session_factory, plain, alice, 8 * DAY)
    edge = await _message(session_factory, plain, bob, 7 * DAY)  # exactly at the cutoff
    recent = await _message(session_factory, plain, bob, 6 * DAY)
    kept_for_report = await _message(session_factory, reported, bob, 30 * DAY)
    report_closed = await _message(session_factory, closed, bob, 30 * DAY)
    await _report(session_factory, reported, alice, bob, closed=False)
    await _report(session_factory, closed, alice, bob, closed=True)
    async with session_factory() as session:
        session.add(UserBlock(blocker_user_id=alice, blocked_user_id=bob, conversation_id=plain))
        await session.commit()

    assert await retention.run(NOW) == 2
    remaining = await _remaining(session_factory)
    assert remaining == {edge, recent, kept_for_report}
    assert not {old, report_closed} & remaining
    # Conversations, participants, reports, and blocks are untouched.
    assert await _count(session_factory, Conversation) == 3
    assert await _count(session_factory, ConversationParticipant) == 6
    assert await _count(session_factory, Report) == 2
    assert await _count(session_factory, UserBlock) == 1


async def test_changing_the_setting_changes_the_next_run(
    retention: ChatRetention, session_factory: Sessions
) -> None:
    alice, bob = await _people(session_factory)
    conversation = await _conversation(session_factory, alice, bob)
    five_days = await _message(session_factory, conversation, alice, 5 * DAY)
    two_days = await _message(session_factory, conversation, alice, 2 * DAY)
    assert await retention.run(NOW) == 0  # default 7 days
    await _set_days(session_factory, 3)
    assert await retention.run(NOW) == 1
    assert await _remaining(session_factory) == {two_days}
    assert five_days not in await _remaining(session_factory)


async def test_running_again_is_safe(retention: ChatRetention, session_factory: Sessions) -> None:
    alice, bob = await _people(session_factory)
    conversation = await _conversation(session_factory, alice, bob)
    await _message(session_factory, conversation, alice, 8 * DAY)
    newer = await _message(session_factory, conversation, alice, DAY)
    assert await retention.run(NOW) == 1
    assert await retention.run(NOW) == 0
    assert await _remaining(session_factory) == {newer}


async def test_large_backlogs_are_deleted_in_batches(
    retention: ChatRetention, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "BATCH_SIZE", 2)
    alice, bob = await _people(session_factory)
    conversation = await _conversation(session_factory, alice, bob)
    for _ in range(5):
        await _message(session_factory, conversation, alice, 10 * DAY)
    assert await retention.run(NOW) == 5
    assert await _remaining(session_factory) == set()


async def test_a_message_being_written_is_left_for_the_next_run(
    retention: ChatRetention, session_factory: Sessions
) -> None:
    alice, bob = await _people(session_factory)
    conversation = await _conversation(session_factory, alice, bob)
    busy = await _message(session_factory, conversation, alice, 10 * DAY)
    other = await _message(session_factory, conversation, alice, 10 * DAY)
    async with session_factory() as holding:
        await holding.execute(
            select(ChatMessage.id).where(ChatMessage.id == busy).with_for_update()
        )
        assert await retention.run(NOW) == 1  # does not wait for the lock
        await holding.commit()
    assert await _remaining(session_factory) == {busy}
    assert other not in await _remaining(session_factory)
    assert await retention.run(NOW) == 1


async def test_the_job_runs_once_a_day(
    retention: ChatRetention, redis: Redis, session_factory: Sessions
) -> None:
    alice, bob = await _people(session_factory)
    conversation = await _conversation(session_factory, alice, bob)
    await _message(session_factory, conversation, alice, 8 * DAY)
    assert await retention.tick(NOW) == 1
    await _message(session_factory, conversation, alice, 8 * DAY)
    assert await retention.tick(NOW + timedelta(hours=1)) is None  # not due
    ttl = await redis.ttl(RUN_KEY)
    assert 86_000 < ttl <= 86_400
    await redis.delete(RUN_KEY)  # a day later the key has expired
    assert await retention.tick(NOW + DAY) == 1


class BrokenSettings(RuntimeSettings):
    async def get(self, session: AsyncSession, key: SettingKey) -> int:
        raise RuntimeError("database unavailable")


async def test_a_failed_run_is_retried_within_the_hour(
    redis: Redis, session_factory: Sessions
) -> None:
    retention = ChatRetention(redis, session_factory, BrokenSettings())
    with pytest.raises(RuntimeError):
        await retention.tick(NOW)
    assert 0 < await redis.ttl(RUN_KEY) <= 3_600


async def test_only_counts_are_logged(
    retention: ChatRetention, session_factory: Sessions, caplog: pytest.LogCaptureFixture
) -> None:
    alice, bob = await _people(session_factory)
    conversation = await _conversation(session_factory, alice, bob)
    await _message(session_factory, conversation, alice, 8 * DAY)
    with caplog.at_level(logging.INFO, logger=rt.__name__):
        await retention.run(NOW)
    (record,) = [r for r in caplog.records if r.name == rt.__name__]
    assert record.count == 1  # type: ignore[attr-defined]
    assert "private words" not in caplog.text


async def test_a_run_interrupted_midway_is_retried_within_the_hour(
    redis: Redis, session_factory: Sessions
) -> None:
    # While a run is in progress the key lasts only an hour, so if the worker dies
    # mid-run the next attempt comes within the hour, not a day later.
    seen: list[int] = []

    class Watched(ChatRetention):
        async def run(self, now: datetime, *, renew_lease: bool = False) -> int:
            seen.append(await redis.ttl(RUN_KEY))
            return await super().run(now, renew_lease=renew_lease)

    retention = Watched(redis, session_factory, RuntimeSettings(ttl_seconds=0))
    assert await retention.tick(NOW) == 0
    assert 0 < seen[0] <= 3_600
    assert await redis.ttl(RUN_KEY) > 86_000  # a full day once it succeeded


async def test_a_long_run_keeps_its_claim(
    redis: Redis, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key's hour is renewed after every batch, so a run longer than an hour is
    # never joined by a second one.
    monkeypatch.setattr(rt, "BATCH_SIZE", 1)
    alice, bob = await _people(session_factory)
    conversation = await _conversation(session_factory, alice, bob)
    for _ in range(3):
        await _message(session_factory, conversation, alice, 8 * DAY)
    real = rt.delete_expired_messages
    seen: list[int] = []

    async def batch(session: AsyncSession, cutoff: datetime, limit: int) -> int:
        seen.append(await redis.ttl(RUN_KEY))
        await redis.delete(RUN_KEY)  # as if the hour had just run out
        return await real(session, cutoff, limit)

    monkeypatch.setattr(rt, "delete_expired_messages", batch)
    retention = ChatRetention(redis, session_factory, RuntimeSettings(ttl_seconds=0))
    assert await retention.tick(NOW) == 3
    # Before every batch after the first, the claim was there again.
    assert all(0 < ttl <= 3_600 for ttl in seen[1:]) and len(seen) == 4
