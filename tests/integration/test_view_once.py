"""View-once media in Private mode (D-048)."""

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from aiogram.methods import CopyMessage, EditMessageCaption, EditMessageText, SendMessage
from aiogram.types import Document, PhotoSize, Video, Voice
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import private as pv
from apps.bot.keyboards import END_CHAT, PRIVATE_OFF, PRIVATE_ON
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import private
from backend.modules.chat.models import ChatMessage, TelegramDeletion
from backend.modules.chat.private import PrivateModeJob
from tests.support.chat import USER_ID, Chat
from tests.support.db import wait_until_blocked
from tests.support.telegram import content_update, edited_update
from tests.support.world import BOB_ID, World, match, onboarded_world

Sessions = async_sessionmaker[AsyncSession]
_message_ids = count(90_000)
PHOTO = [PhotoSize(file_id="photo-1", file_unique_id="p1", width=800, height=800)]


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    world = await onboarded_world(bot_settings, redis, session_factory)
    await match(world)
    return world


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


async def queued(sessions: Sessions) -> dict[tuple[int, int], datetime]:
    async with sessions() as session:
        rows = (await session.execute(select(TelegramDeletion))).scalars()
        return {(r.chat_id, r.message_id): r.not_before for r in rows}


def placeholders_to_bob(world: World) -> list[SendMessage]:
    return [
        m
        for m in world.alice.session.requests
        if isinstance(m, SendMessage)
        and int(m.chat_id) == BOB_ID
        and pv.VIEW_BUTTON in str(m.reply_markup)
    ]


async def open_it(world: World, row_id: int) -> str:
    """Bob taps 👁 View on the placeholder for stored message `row_id`."""
    await world.bob.press_data(pv.ViewOnceAction(message=row_id).pack())
    answers = world.bob.session.callback_answers()
    return answers[-1] if answers else ""


def opened_copies(world: World) -> list[CopyMessage]:
    return [m for m in world.bob.session.requests if isinstance(m, CopyMessage)]


# --- Delivery --------------------------------------------------------------------------


async def test_private_mode_photos_arrive_as_a_tap_to_view_placeholder(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, photo=PHOTO, caption="look")
    assert world.alice.session.copies() == []  # not copied directly
    (placeholder,) = placeholders_to_bob(world)
    assert placeholder.text == ("📷 Photo · tap to view. It disappears 20 seconds after opening.")
    assert placeholder.protect_content is True
    assert "look" not in placeholder.text  # the caption shows only when opened
    (row,) = await stored(session_factory)
    # Stored for moderation as usual, with the placeholder as the partner's copy.
    assert (row.view_once, row.kind, row.text, row.file_id) == (True, "photo", "look", "photo-1")
    assert row.recipient_message_id == world.alice.session.requests.index(placeholder) + 1


@pytest.mark.parametrize(
    ("fields", "label"),
    [
        ({"voice": Voice(file_id="v", file_unique_id="v", duration=45)}, "🎤 Voice message (0:45)"),
        (
            {"video": Video(file_id="m", file_unique_id="m", width=1, height=1, duration=130)},
            "🎬 Video (2:10)",
        ),
    ],
)
async def test_long_media_get_their_length_added(
    world: World, fields: dict[str, Any], label: str
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, **fields)
    (placeholder,) = placeholders_to_bob(world)
    # The length is shown, and added to the 20-second timer.
    total = "65 seconds" if "voice" in fields else "2 minutes 30 seconds"
    assert placeholder.text == f"{label} · tap to view. It disappears {total} after opening."


async def test_other_kinds_and_normal_chats_are_unchanged(
    world: World, session_factory: Sessions
) -> None:
    await send(world.alice, photo=PHOTO)  # Private mode off
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, text="hello")
    await send(world.alice, document=Document(file_id="d", file_unique_id="d"))
    assert len(world.alice.session.copies()) == 3
    assert placeholders_to_bob(world) == []
    assert [r.view_once for r in await stored(session_factory)] == [False, False, False]


# --- Opening ---------------------------------------------------------------------------


async def test_opening_sends_it_once_then_deletes_everything_after_the_timer(
    world: World, session_factory: Sessions, deletions: pv.TelegramDeletions
) -> None:
    await world.alice.send(PRIVATE_ON)
    sent = await send(world.alice, photo=PHOTO, caption="look")
    (row,) = await stored(session_factory)
    before = datetime.now(UTC)
    assert await open_it(world, row.id) == ""
    (copy,) = opened_copies(world)
    assert (int(copy.chat_id), int(copy.from_chat_id), copy.message_id) == (BOB_ID, USER_ID, sent)
    assert copy.protect_content is True
    copy_id = 10_000 + world.bob.session.requests.index(copy) + 1
    edits = [m for m in world.bob.session.requests if isinstance(m, EditMessageText)]
    assert edits[-1].text == pv.OPENED.format(total="20 seconds")
    assert edits[-1].message_id == row.recipient_message_id

    places = await queued(session_factory)
    expected = {(BOB_ID, copy_id), (BOB_ID, row.recipient_message_id), (USER_ID, sent)}
    assert set(places) == expected
    for not_before in places.values():
        assert before + timedelta(seconds=19) < not_before < before + timedelta(seconds=25)
    assert await deletions.tick(before + timedelta(seconds=10)) == 0
    assert await deletions.tick(before + timedelta(seconds=30)) == 3
    assert set(world.worker.deleted) == expected
    # Still stored for moderation.
    assert [r.file_id for r in await stored(session_factory)] == ["photo-1"]


async def test_voice_waits_for_its_length_plus_the_timer(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, voice=Voice(file_id="v", file_unique_id="v", duration=45))
    (row,) = await stored(session_factory)
    before = datetime.now(UTC)
    await open_it(world, row.id)
    for not_before in (await queued(session_factory)).values():
        assert before + timedelta(seconds=64) < not_before < before + timedelta(seconds=70)


async def test_the_timer_is_an_admin_setting(world: World, session_factory: Sessions) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.VIEW_ONCE_SECONDS.value, value=60))
        await session.commit()
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, photo=PHOTO)
    (placeholder,) = placeholders_to_bob(world)
    assert "disappears 60 seconds after opening" in placeholder.text
    (row,) = await stored(session_factory)
    before = datetime.now(UTC)
    await open_it(world, row.id)
    for not_before in (await queued(session_factory)).values():
        assert before + timedelta(seconds=59) < not_before < before + timedelta(seconds=65)


async def test_it_opens_only_once_and_only_for_the_partner(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, photo=PHOTO)
    (row,) = await stored(session_factory)
    await world.alice.press_data(pv.ViewOnceAction(message=row.id).pack())
    assert world.alice.session.callback_answers()[-1] == pv.NOT_YOURS  # the sender
    await open_it(world, row.id)
    assert await open_it(world, row.id) == pv.ALREADY_VIEWED
    assert len(opened_copies(world)) == 1
    assert await open_it(world, 999_999) == pv.NOT_YOURS


@pytest.mark.parametrize("ending", [PRIVATE_OFF, END_CHAT])
async def test_unopened_media_is_gone_when_private_mode_or_the_chat_ends(
    world: World,
    session_factory: Sessions,
    deletions: pv.TelegramDeletions,
    ending: str,
) -> None:

    await world.alice.send(PRIVATE_ON)
    sent = await send(world.alice, photo=PHOTO)
    (row,) = await stored(session_factory)
    await world.alice.send(ending)
    job = PrivateModeJob(
        session_factory,
        RuntimeSettings(ttl_seconds=0),
        pv.PrivateModeNotifier(world.worker_bot, session_factory),
    )
    await job.tick(datetime.now(UTC))
    assert await open_it(world, row.id) == pv.UNAVAILABLE
    assert opened_copies(world) == []
    await deletions.tick(datetime.now(UTC))
    assert {(USER_ID, sent), (BOB_ID, row.recipient_message_id)} <= set(world.worker.deleted)


async def test_media_the_sender_deleted_cannot_be_opened(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(PRIVATE_ON)
    sent = await send(world.alice, photo=PHOTO)
    (row,) = await stored(session_factory)
    world.bob.session.fail_copies("Bad Request: message to copy not found")
    await open_it(world, row.id)
    assert world.bob.session.sent_texts()[-1] == pv.COULD_NOT_OPEN
    # The placeholder and the original are still cleaned up after the timer.
    assert {(BOB_ID, row.recipient_message_id), (USER_ID, sent)} == set(
        await queued(session_factory)
    )


async def test_a_caption_edit_is_kept_but_not_sent_to_the_placeholder(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(PRIVATE_ON)
    sent = await send(world.alice, photo=PHOTO, caption="first")
    await world.alice.dispatcher.feed_update(
        world.alice.bot,
        edited_update(next(Chat._ids), USER_ID, sent, photo=PHOTO, caption="second"),
    )
    assert not [m for m in world.alice.session.requests if isinstance(m, EditMessageCaption)]
    (row,) = await stored(session_factory)
    assert (row.text, row.edited_text) == ("first", "second")


def test_the_note_mentions_view_once() -> None:
    assert "can be opened once" in pv.NOTE


async def test_a_tap_while_private_mode_is_turned_off_waits_and_is_refused(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(PRIVATE_ON)
    await send(world.alice, photo=PHOTO)
    (row,) = await stored(session_factory)
    alice = await world.user_id(USER_ID)
    async with session_factory() as turning_off:
        toggled = await private.turn_off(turning_off, row.conversation_id, alice, datetime.now(UTC))
        assert toggled is not None and toggled.changed
        tap = asyncio.create_task(open_it(world, row.id))
        await wait_until_blocked(session_factory)
        await turning_off.commit()
        assert await tap == pv.UNAVAILABLE
    assert opened_copies(world) == []
