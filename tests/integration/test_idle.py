"""Quiet chats are warned, then ended (D-039)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import SendMessage
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import chat as ch
from apps.bot.endings import IDLE_ENDED
from backend.config.settings import BotSettings
from backend.modules.admin import service as admin_service
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.chat.idle import IdleChats
from backend.modules.chat.models import Conversation
from tests.support.chat import USER_ID
from tests.support.world import (
    BOB_ID,
    World,
    assert_chat_ended,
    match,
    onboarded_world,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
QUIET = timedelta(seconds=300)
GRACE = timedelta(seconds=60)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    world = await onboarded_world(bot_settings, redis, session_factory)
    await match(world)
    return world


@pytest.fixture
def idle(world: World, session_factory: Sessions) -> IdleChats:
    """The worker's idle job, sending through the same simulated worker bot."""
    return IdleChats(session_factory, RuntimeSettings(ttl_seconds=0), world.notifier)


async def _conversation(sessions: Sessions) -> Conversation:
    async with sessions() as session:
        return (await session.execute(select(Conversation))).scalar_one()


async def _set(sessions: Sessions, **values: object) -> None:
    async with sessions() as session:
        await session.execute(update(Conversation).values(**values))
        await session.commit()


def texts_to(world: World, chat_id: int) -> list[str]:
    return [text_of(m) for m in world.worker.sent_to(chat_id) if isinstance(m, SendMessage)]


# --- Service -------------------------------------------------------------------------


async def test_quiet_chats_are_warned_once(world: World, session_factory: Sessions) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        assert await chat.warn_idle_chats(session, now, QUIET, 10) == []  # just started
    await _set(session_factory, last_activity_at=now - QUIET)
    async with session_factory() as session:
        [warned] = await chat.warn_idle_chats(session, now, QUIET, 10)
        await session.commit()
    assert sorted(warned.user_ids) == sorted(
        [await world.user_id(USER_ID), await world.user_id(BOB_ID)]
    )
    async with session_factory() as session:
        assert await chat.warn_idle_chats(session, now, QUIET, 10) == []  # already warned


async def test_warned_chats_end_after_the_grace_time(
    world: World, session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    await _set(
        session_factory, last_activity_at=now - QUIET - GRACE, idle_warned_at=now - GRACE / 2
    )
    async with session_factory() as session:
        assert await chat.end_idle_chats(session, now, GRACE, 10) == []  # grace not over
    async with session_factory() as session:
        [ended] = await chat.end_idle_chats(session, now + GRACE, GRACE, 10)
        await session.commit()
    conversation = await _conversation(session_factory)
    assert (conversation.end_reason, conversation.ended_by_user_id) == ("idle", None)
    async with session_factory() as session:
        assert await chat.active_chat(session, ended.user_ids[0]) is None


async def test_a_message_clears_the_warning(world: World, session_factory: Sessions) -> None:
    now = datetime.now(UTC)
    await _set(session_factory, last_activity_at=now - QUIET, idle_warned_at=now - GRACE)
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        active = await chat.active_chat(session, alice)
        assert active is not None
        assert await chat.claim_message(session, active, alice, 1, "text", "hi", None)
        await session.commit()
    conversation = await _conversation(session_factory)
    assert conversation.idle_warned_at is None
    async with session_factory() as session:
        assert await chat.end_idle_chats(session, now + GRACE, GRACE, 10) == []


async def test_a_message_in_flight_is_never_cut_off(
    world: World, session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    await _set(session_factory, last_activity_at=now - QUIET, idle_warned_at=now - GRACE)
    alice = await world.user_id(USER_ID)
    async with session_factory() as sending:
        active = await chat.active_chat(sending, alice)
        assert active is not None
        assert await chat.claim_message(sending, active, alice, 2, "text", "hi", None)
        # The idle job runs while the message holds the chat's lock: it skips the chat.
        async with session_factory() as job:
            assert await chat.end_idle_chats(job, now + GRACE, GRACE, 10) == []
        await sending.commit()
    assert (await _conversation(session_factory)).ended_at is None


async def test_concurrent_jobs_warn_once(world: World, session_factory: Sessions) -> None:
    now = datetime.now(UTC)
    await _set(session_factory, last_activity_at=now - QUIET)

    async def warn() -> int:
        async with session_factory() as session:
            warned = await chat.warn_idle_chats(session, now, QUIET, 10)
            await session.commit()
            return len(warned)

    assert sorted(await asyncio.gather(warn(), warn())) == [0, 1]


# --- Bot -----------------------------------------------------------------------------


async def test_both_users_are_warned_then_the_chat_ends(
    world: World, idle: IdleChats, session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    await _set(session_factory, last_activity_at=now - QUIET)
    assert await idle.tick(now) == (1, 0)
    warning = ch.IDLE_WARNING.format(quiet="5 minutes", grace="60 seconds")
    assert texts_to(world, USER_ID)[-1] == texts_to(world, BOB_ID)[-1] == warning

    assert await idle.tick(now + GRACE) == (0, 1)
    for chat_id in (USER_ID, BOB_ID):
        assert_chat_ended(world.worker, chat_id, IDLE_ENDED)


async def test_a_message_after_the_warning_keeps_the_chat(
    world: World, idle: IdleChats, session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    await _set(session_factory, last_activity_at=now - QUIET)
    await idle.tick(now)
    await world.alice.send("Still here!")
    assert await idle.tick(now + GRACE) == (0, 0)
    assert (await _conversation(session_factory)).ended_at is None


async def test_the_times_are_admin_settings(
    world: World, idle: IdleChats, session_factory: Sessions
) -> None:
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        await admin_service.set_role(session, USER_ID, "admin")
        await admin_service.change_setting(session, alice, SettingKey.CHAT_IDLE_NOTICE_SECONDS, 600)
        await admin_service.change_setting(session, alice, SettingKey.CHAT_IDLE_GRACE_SECONDS, 90)
        await session.commit()
    now = datetime.now(UTC)
    await _set(session_factory, last_activity_at=now - QUIET)
    assert await idle.tick(now) == (0, 0)  # 5 minutes is no longer enough
    await _set(session_factory, last_activity_at=now - 2 * QUIET)
    assert await idle.tick(now) == (1, 0)
    assert texts_to(world, USER_ID)[-1] == ch.IDLE_WARNING.format(
        quiet="10 minutes", grace="90 seconds"
    )
    assert await idle.tick(now + GRACE) == (0, 0)  # 60 seconds is not the grace any more
    assert await idle.tick(now + timedelta(seconds=90)) == (0, 1)


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (1, "1 second"),
        (60, "60 seconds"),
        (90, "90 seconds"),
        (300, "5 minutes"),
        (150, "2 minutes 30 seconds"),
    ],
)
def test_durations_in_words(seconds: int, words: str) -> None:
    assert ch.duration(seconds) == words


async def test_an_undelivered_message_does_not_keep_the_chat_alive(
    world: World, session_factory: Sessions
) -> None:
    warned_at = datetime.now(UTC) - GRACE / 2
    await _set(session_factory, last_activity_at=warned_at - QUIET, idle_warned_at=warned_at)
    world.alice.session.fail_copies("Bad Request: message can't be copied")
    await world.alice.send("this kind can't be copied")
    conversation = await _conversation(session_factory)
    assert conversation.idle_warned_at == warned_at
    assert conversation.last_activity_at == warned_at - QUIET


async def test_undoing_activity_keeps_a_later_message(
    world: World, session_factory: Sessions
) -> None:
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        active = await chat.active_chat(session, alice)
        assert active is not None
        first = await chat.claim_message(session, active, alice, 11, "text", "a", None)
        await session.commit()
    async with session_factory() as session:
        assert await chat.claim_message(session, active, alice, 12, "text", "b", None)
        await session.commit()
        later = (await _conversation(session_factory)).last_activity_at
    assert isinstance(first, chat.Claim)
    async with session_factory() as session:
        await chat.discard_message(session, first)  # the first failed after the second
        await session.commit()
    assert (await _conversation(session_factory)).last_activity_at == later
