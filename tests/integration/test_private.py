"""Private mode (D-031, D-047)."""

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import DeleteMessages
from aiogram.types import Dice
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import private as pv
from apps.bot.keyboards import END_CHAT, PRIVATE_OFF, PRIVATE_ON
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import private
from backend.modules.chat import service as chat_service
from backend.modules.chat.models import ChatMessage, PrivateSession, TelegramDeletion
from backend.modules.chat.private import PrivateModeJob
from tests.support.chat import USER_ID, Chat
from tests.support.db import wait_until_blocked
from tests.support.telegram import content_update
from tests.support.world import (
    BOB_ID,
    CHAT_KEYS,
    PRIVATE_CHAT_KEYS,
    World,
    keys,
    last_to,
    match,
    onboarded_world,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
_message_ids = count(70_000)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    world = await onboarded_world(bot_settings, redis, session_factory)
    await match(world)
    return world


@pytest.fixture
def job(world: World, session_factory: Sessions) -> PrivateModeJob:
    notifier = pv.PrivateModeNotifier(world.worker_bot, session_factory)
    return PrivateModeJob(session_factory, RuntimeSettings(ttl_seconds=0), notifier)


@pytest.fixture
def deletions(world: World, session_factory: Sessions) -> pv.TelegramDeletions:
    return pv.TelegramDeletions(world.worker_bot, session_factory)


async def send(chat: Chat, **fields: Any) -> int:
    message_id = next(_message_ids)
    await chat.dispatcher.feed_update(
        chat.bot, content_update(next(Chat._ids), chat.user_id, message_id, **fields)
    )
    return message_id


async def stored(sessions: Sessions) -> list[ChatMessage]:
    async with sessions() as session:
        return list((await session.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars())


async def queued(sessions: Sessions) -> list[TelegramDeletion]:
    async with sessions() as session:
        return list(
            (
                await session.execute(select(TelegramDeletion).order_by(TelegramDeletion.id))
            ).scalars()
        )


async def both_copies(world: World, messages: list[ChatMessage]) -> set[tuple[int, int]]:
    """Where each message is in Telegram: the sender's original and the partner's copy."""
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    chat_of = {alice: USER_ID, bob: BOB_ID}
    places = set()
    for m in messages:
        partner = bob if m.sender_user_id == alice else alice
        places.add((chat_of[m.sender_user_id], m.sender_message_id))
        if m.recipient_message_id is not None:
            places.add((chat_of[partner], m.recipient_message_id))
    return places


# --- Turning it on and off -------------------------------------------------------------


async def test_turning_it_on_tells_both_and_changes_both_keyboards(
    world: World, session_factory: Sessions
) -> None:
    assert await world.alice.send(PRIVATE_ON) == pv.YOU_TURNED_ON
    assert keys(last_to(world.alice.session, USER_ID)) == PRIVATE_CHAT_KEYS
    to_bob = last_to(world.alice.session, BOB_ID)
    assert (text_of(to_bob), keys(to_bob)) == (pv.PARTNER_TURNED_ON, PRIVATE_CHAT_KEYS)
    assert world.alice.session.copies() == []  # the button is never relayed
    assert await world.alice.send(PRIVATE_ON) == pv.ALREADY_ON
    async with session_factory() as session:
        assert len((await session.execute(select(PrivateSession))).all()) == 1


async def test_either_user_can_turn_it_off(world: World, session_factory: Sessions) -> None:
    await world.alice.send(PRIVATE_ON)
    assert await world.bob.send(PRIVATE_OFF) == pv.YOU_TURNED_OFF
    assert keys(last_to(world.bob.session, BOB_ID)) == CHAT_KEYS
    to_alice = last_to(world.bob.session, USER_ID)
    assert (text_of(to_alice), keys(to_alice)) == (pv.PARTNER_TURNED_OFF, CHAT_KEYS)
    assert await world.bob.send(PRIVATE_OFF) == pv.NOT_ON
    async with session_factory() as session:
        (private,) = (await session.execute(select(PrivateSession))).scalars()
        bob = await world.user_id(BOB_ID)
        assert (private.end_reason, private.ended_by_user_id) == ("turned_off", bob)


async def test_outside_a_chat_it_does_nothing(world: World, session_factory: Sessions) -> None:
    await world.alice.send(END_CHAT)
    assert await world.alice.send(PRIVATE_ON) == pv.NOT_IN_CHAT
    async with session_factory() as session:
        assert (await session.execute(select(PrivateSession))).all() == []


# --- Deleting the messages -------------------------------------------------------------


async def test_turning_it_off_deletes_both_copies_of_its_messages_only(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await send(world.alice, text="before")
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, text="private one")
    await send(world.bob, text="private two")
    await world.alice.send(PRIVATE_OFF)
    await send(world.alice, text="after")
    messages = await stored(session_factory)
    private = [m for m in messages if m.private_session_id is not None]
    assert [m.text for m in private] == ["private one", "private two"]

    assert await deletions.tick(datetime.now(UTC)) == 4
    assert set(world.worker.deleted) == await both_copies(world, private)
    assert await queued(session_factory) == []
    # Still stored for moderation, with normal retention.
    assert [m.text for m in await stored(session_factory)] == [
        "before",
        "private one",
        "private two",
        "after",
    ]
    # Deleted in one call per chat.
    calls = [r for r in world.worker.requests if isinstance(r, DeleteMessages)]
    assert sorted(int(c.chat_id) for c in calls) == [USER_ID, BOB_ID]


@pytest.mark.parametrize("ending", ["end chat", "partner ends chat"])
async def test_ending_the_chat_deletes_them_too(
    world: World,
    session_factory: Sessions,
    job: PrivateModeJob,
    deletions: pv.TelegramDeletions,
    ending: str,
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, text="secret-ish")
    await (world.alice if ending == "end chat" else world.bob).send(END_CHAT)
    assert await job.tick(datetime.now(UTC)) == 1
    for chat_id in (USER_ID, BOB_ID):
        assert text_of(last_to(world.worker, chat_id)) == pv.CHAT_ENDED
    assert await deletions.tick(datetime.now(UTC)) == 2
    assert set(world.worker.deleted) == await both_copies(world, await stored(session_factory))
    async with session_factory() as session:
        (private,) = (await session.execute(select(PrivateSession))).scalars()
        assert private.end_reason == "chat_ended"
    assert await job.tick(datetime.now(UTC)) == 0  # ended once


async def test_it_turns_itself_off_at_the_time_limit(
    world: World, session_factory: Sessions, job: PrivateModeJob
) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.PRIVATE_MODE_HOURS.value, value=2))
        await session.commit()
    await world.alice.send(PRIVATE_ON)
    now = datetime.now(UTC)
    assert await job.tick(now + timedelta(hours=1, minutes=59)) == 0
    assert await job.tick(now + timedelta(hours=2, seconds=1)) == 1
    for chat_id in (USER_ID, BOB_ID):
        notice = last_to(world.worker, chat_id)
        assert (text_of(notice), keys(notice)) == (pv.TIME_LIMIT, CHAT_KEYS)
    # It can be turned on again.
    assert await world.alice.send(PRIVATE_ON) == pv.YOU_TURNED_ON


async def test_the_time_limit_stays_within_telegrams_window() -> None:
    from backend.modules.admin.settings import SPECS

    assert SPECS[SettingKey.PRIVATE_MODE_HOURS].maximum < 48
    assert SPECS[SettingKey.PRIVATE_MODE_HOURS].default == 24


# --- Dice ------------------------------------------------------------------------------


async def test_dice_are_relayed_and_deleted_after_24_hours(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, dice=Dice(emoji="🎲", value=5))
    await send(world.alice, text="hello")
    assert len(world.alice.session.copies()) == 1 + 1  # the dice is relayed as usual
    assert await world.alice.send(PRIVATE_OFF) == f"{pv.YOU_TURNED_OFF}\n\n{pv.DICE_LATER}"
    sent_at = (await stored(session_factory))[0].created_at

    assert await deletions.tick(datetime.now(UTC)) == 2  # the text, at once
    assert await deletions.tick(sent_at + timedelta(hours=23)) == 0  # dice not yet
    assert len(await queued(session_factory)) == 2
    assert await deletions.tick(sent_at + timedelta(hours=24, minutes=6)) == 2
    assert set(world.worker.deleted) == await both_copies(world, await stored(session_factory))


async def test_nothing_stays_queued_past_48_hours(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, dice=Dice(emoji="🎲", value=2))
    await world.alice.send(PRIVATE_OFF)
    sent_at = (await stored(session_factory))[0].created_at
    assert await deletions.tick(sent_at + timedelta(hours=48)) == 0
    assert await queued(session_factory) == []
    assert world.worker.deleted == []  # never tried once Telegram would refuse


# --- Deletion failures -----------------------------------------------------------------


async def _queue_three(world: World) -> None:
    await world.alice.send(PRIVATE_ON)
    for text in ("a", "b", "c"):
        await send(world.alice, text=text)
    await world.alice.send(PRIVATE_OFF)


async def test_a_message_that_cant_be_deleted_does_not_stop_the_rest(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await _queue_three(world)
    first = (await stored(session_factory))[0]
    gone = (USER_ID, first.sender_message_id)
    world.worker.undeletable.add(gone)  # fails the batch call, then its own call
    assert await deletions.tick(datetime.now(UTC)) == 6  # all settled
    assert set(world.worker.deleted) == await both_copies(world, await stored(session_factory)) - {
        gone
    }
    assert await queued(session_factory) == []


async def test_a_user_who_blocked_the_bot_is_skipped(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await _queue_three(world)
    world.worker.bulk_delete_error = TelegramForbiddenError(
        method=DeleteMessages(chat_id=USER_ID, message_ids=[1]), message="Forbidden"
    )
    assert await deletions.tick(datetime.now(UTC)) == 6
    assert await queued(session_factory) == []


async def test_a_request_to_slow_down_keeps_them_queued(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await _queue_three(world)
    world.worker.bulk_delete_error = TelegramRetryAfter(
        method=DeleteMessages(chat_id=USER_ID, message_ids=[1]), message="Too Many", retry_after=300
    )
    now = datetime.now(UTC)
    assert await deletions.tick(now) == 0
    assert len(await queued(session_factory)) == 6
    world.worker.bulk_delete_error = None
    # They wait as long as Telegram asked (5 minutes), not just the one-minute lease.
    assert await deletions.tick(now + timedelta(minutes=2)) == 0
    assert await deletions.tick(now + timedelta(minutes=6)) == 6


# --- Messages in flight ----------------------------------------------------------------


async def test_a_message_in_flight_when_it_is_turned_off_is_deleted_too(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await world.alice.send(PRIVATE_ON)

    async def turned_off_meanwhile() -> None:
        world.alice.session.during_copy = None
        await world.bob.send(PRIVATE_OFF)

    world.alice.session.during_copy = turned_off_meanwhile
    await send(world.alice, text="just in time")
    assert await deletions.tick(datetime.now(UTC)) == 2
    assert set(world.worker.deleted) == await both_copies(world, await stored(session_factory))


async def test_a_message_in_flight_when_the_chat_ends_is_deleted_too(
    world: World, session_factory: Sessions, job: PrivateModeJob, deletions: pv.TelegramDeletions
) -> None:
    await world.alice.send(PRIVATE_ON)
    message_id = next(_message_ids)

    async def chat_ends_meanwhile() -> None:
        world.alice.session.during_copy = None
        await world.bob.send(END_CHAT)

    world.alice.session.during_copy = chat_ends_meanwhile
    await world.alice.dispatcher.feed_update(
        world.alice.bot, content_update(next(Chat._ids), USER_ID, message_id, text="late")
    )
    (copy,) = world.alice.session.copies()
    copy_id = 10_000 + world.alice.session.requests.index(copy) + 1
    await job.tick(datetime.now(UTC))
    await deletions.tick(datetime.now(UTC))
    # The relay tried to withdraw the copy itself; both are also queued, so the copy is
    # retried even if that withdrawal failed.
    assert {(USER_ID, message_id), (BOB_ID, copy_id)} <= set(world.worker.deleted)


async def test_ending_at_the_time_limit_waits_for_a_copy_being_linked(
    world: World, session_factory: Sessions, job: PrivateModeJob
) -> None:
    await world.alice.send(PRIVATE_ON)
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        active = await chat_service.active_chat(session, alice)
        assert active is not None
        claim = await chat_service.claim_message(
            session, active, alice, next(_message_ids), "text", "linking", None
        )
        assert claim is not None
        await session.execute(
            update(PrivateSession).values(started_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await session.commit()
    async with session_factory() as linking:
        # The copy is being linked (not yet committed) as the time limit is reached.
        assert await chat_service.complete_message(linking, claim, 4_321)
        task = asyncio.create_task(job.tick(datetime.now(UTC)))
        await wait_until_blocked(session_factory)
        await linking.commit()
        assert await task == 1
    places = {(d.chat_id, d.message_id) for d in await queued(session_factory)}
    assert (BOB_ID, 4_321) in places  # the copy was seen and queued


# --- Honest wording --------------------------------------------------------------------


def test_no_secret_chat_or_encryption_claims() -> None:
    texts = [value for name, value in vars(pv).items() if name.isupper() and isinstance(value, str)]
    for text in (t.casefold() for t in texts):
        # Each of these appears only as a plain denial.
        assert text.count("secret chat") == text.count("isn't a telegram secret chat")
        assert text.count("encrypted") == text.count("isn't end-to-end encrypted")
        # Screen capture is described as blocked only in Telegram's own apps.
        blocked = "screenshots, screen recording, saving and forwarding are blocked in telegram's"
        assert text.count("screenshot") == text.count(blocked)
    note = pv.NOTE.casefold()
    for denial in (
        "isn't a telegram secret chat",
        "isn't end-to-end encrypted",
        "nothing can stop someone photographing the screen",
    ):
        assert denial in note
    assert "keeps a copy for moderation" in note


def test_the_queue_holds_no_message_content() -> None:
    columns = set(TelegramDeletion.__table__.columns.keys())
    assert columns == {"id", "chat_id", "message_id", "not_before", "expires_at", "created_at"}


async def test_a_session_ended_by_two_callers_at_once_ends_once(
    world: World, session_factory: Sessions
) -> None:
    # E.g. a user turns it off just as the worker reaches the time limit.
    await world.alice.send(PRIVATE_ON)
    async with session_factory() as session:
        private_id = (await session.execute(select(PrivateSession.id))).scalar_one()
    now = datetime.now(UTC)
    async with session_factory() as first:
        assert await private.end_session(first, private_id, now, "turned_off") is not None

        async def second() -> object:
            async with session_factory() as session:
                ended = await private.end_session(session, private_id, now, "time_limit")
                await session.commit()
                return ended

        task = asyncio.create_task(second())
        await wait_until_blocked(session_factory)
        await first.commit()
        assert await task is None
    async with session_factory() as session:
        (row,) = (await session.execute(select(PrivateSession))).scalars()
        assert row.end_reason == "turned_off"
