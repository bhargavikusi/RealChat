"""Asking before sending media right after Private mode ended (D-049)."""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from aiogram.types import PhotoSize, Voice
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import private as pv
from apps.bot import relay as rl
from apps.bot.keyboards import END_CHAT, PRIVATE_OFF, PRIVATE_ON
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat.models import ChatMessage, PrivateSession
from backend.modules.chat.private import PrivateModeJob
from tests.support.chat import USER_ID, Chat
from tests.support.telegram import content_update
from tests.support.world import BOB_ID, World, match, onboarded_world

Sessions = async_sessionmaker[AsyncSession]
_message_ids = count(95_000)
PHOTO = [PhotoSize(file_id="photo-1", file_unique_id="p1", width=800, height=800)]


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    world = await onboarded_world(bot_settings, redis, session_factory)
    await match(world)
    return world


async def send(chat: Chat, **fields: Any) -> int:
    message_id = next(_message_ids)
    await chat.dispatcher.feed_update(
        chat.bot, content_update(next(Chat._ids), chat.user_id, message_id, **fields)
    )
    return message_id


async def stored(sessions: Sessions) -> list[ChatMessage]:
    async with sessions() as session:
        return list((await session.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars())


def to_bob(world: World) -> int:
    """How many messages reached Bob from Alice: copies plus placeholders."""
    copies = [c for c in world.alice.session.copies() if int(c.chat_id) == BOB_ID]
    placeholders = [
        m
        for m in world.alice.session.sent_to(BOB_ID)
        if pv.VIEW_BUTTON in str(getattr(m, "reply_markup", ""))
    ]
    return len(copies) + len(placeholders)


async def partner_turned_it_off(world: World) -> None:
    await world.alice.send(PRIVATE_ON)
    await world.bob.send(PRIVATE_OFF)


# --- When it asks ----------------------------------------------------------------------


async def test_media_right_after_the_partner_turned_it_off_is_held(
    world: World, session_factory: Sessions
) -> None:
    await partner_turned_it_off(world)
    await send(world.alice, photo=PHOTO)
    assert world.alice.last() == rl.HELD_BY_PARTNER.format(kind="photo")
    assert list(world.alice.session.last_buttons()) == [rl.SEND_BUTTON, rl.DROP_BUTTON]
    assert to_bob(world) == 0
    assert await stored(session_factory) == []  # nothing recorded until it is sent


async def test_send_relays_it_once_as_a_normal_message(
    world: World, session_factory: Sessions
) -> None:
    await partner_turned_it_off(world)
    sent = await send(world.alice, photo=PHOTO, caption="hi")
    buttons = world.alice.session.last_buttons()
    await world.alice.press(rl.SEND_BUTTON)
    (copy,) = [c for c in world.alice.session.copies() if int(c.chat_id) == BOB_ID]
    assert (int(copy.from_chat_id), copy.message_id, copy.protect_content) == (USER_ID, sent, True)
    (row,) = await stored(session_factory)
    assert (row.view_once, row.text, row.file_id) == (False, "hi", "photo-1")
    # A second tap, on either button, does nothing more.
    for button in buttons.values():
        await world.alice.press_data(button)
        assert world.alice.session.callback_answers()[-1] == rl.HELD_EXPIRED
    assert to_bob(world) == 1


async def test_dont_send_relays_nothing(world: World, session_factory: Sessions) -> None:
    await partner_turned_it_off(world)
    await send(world.alice, voice=Voice(file_id="v", file_unique_id="v", duration=5))
    assert world.alice.last() == rl.HELD_BY_PARTNER.format(kind="voice message")
    await world.alice.press(rl.DROP_BUTTON)
    assert world.alice.session.callback_answers()[-1] == rl.NOT_SENT
    assert to_bob(world) == 0
    assert await stored(session_factory) == []


async def test_it_asks_after_the_time_limit_too(world: World, session_factory: Sessions) -> None:
    await world.alice.send(PRIVATE_ON)
    async with session_factory() as session:
        await session.execute(
            update(PrivateSession).values(started_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await session.commit()
    job = PrivateModeJob(
        session_factory,
        RuntimeSettings(ttl_seconds=0),
        pv.PrivateModeNotifier(world.worker_bot, session_factory),
    )
    assert await job.tick(datetime.now(UTC)) == 1
    for chat in (world.alice, world.bob):
        await send(chat, photo=PHOTO)
        assert chat.last() == rl.HELD_BY_TIME_LIMIT.format(kind="photo")


# --- When it doesn't -------------------------------------------------------------------


async def test_no_question_when_you_turned_it_off_yourself(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(PRIVATE_ON)
    await world.alice.send(PRIVATE_OFF)
    await send(world.alice, photo=PHOTO)
    assert to_bob(world) == 1
    # Bob didn't turn it off either, but the one who did was his partner: he is asked.
    await send(world.bob, photo=PHOTO)
    assert world.bob.last() == rl.HELD_BY_PARTNER.format(kind="photo")


async def test_no_question_after_the_window(world: World, session_factory: Sessions) -> None:
    await partner_turned_it_off(world)
    async with session_factory() as session:
        await session.execute(
            update(PrivateSession).values(ended_at=datetime.now(UTC) - timedelta(seconds=31))
        )
        await session.commit()
    await send(world.alice, photo=PHOTO)
    assert to_bob(world) == 1


async def test_text_and_other_kinds_are_not_held(world: World) -> None:
    await partner_turned_it_off(world)
    await send(world.alice, text="hello")
    assert to_bob(world) == 1


async def test_the_window_is_an_admin_setting(world: World, session_factory: Sessions) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.PRIVATE_ENDED_ASK_SECONDS.value, value=0))
        await session.commit()
    await partner_turned_it_off(world)
    await send(world.alice, photo=PHOTO)
    assert to_bob(world) == 1  # 0 turns it off


# --- Stale answers ---------------------------------------------------------------------


async def test_send_after_private_mode_is_on_again_sends_nothing(world: World) -> None:
    await partner_turned_it_off(world)
    await send(world.alice, photo=PHOTO)
    send_button = world.alice.session.last_buttons()[rl.SEND_BUTTON]
    await world.bob.send(PRIVATE_ON)
    await world.alice.press_data(send_button)
    assert world.alice.session.callback_answers()[-1] == rl.HELD_EXPIRED
    assert to_bob(world) == 0


async def test_send_after_the_chat_ended_sends_nothing(world: World) -> None:
    await partner_turned_it_off(world)
    await send(world.alice, photo=PHOTO)
    send_button = world.alice.session.last_buttons()[rl.SEND_BUTTON]
    await world.bob.send(END_CHAT)
    await world.alice.press_data(send_button)
    assert world.alice.session.callback_answers()[-1] == rl.HELD_EXPIRED
    assert to_bob(world) == 0


async def test_once_private_mode_is_on_again_media_is_view_once_not_held(
    world: World, session_factory: Sessions
) -> None:
    await partner_turned_it_off(world)
    await world.bob.send(PRIVATE_ON)
    await send(world.alice, photo=PHOTO)
    assert world.alice.last() != rl.HELD_BY_PARTNER.format(kind="photo")
    (row,) = await stored(session_factory)
    assert row.view_once  # sent as a tap-to-view placeholder


async def test_send_in_a_newer_chat_sends_nothing(world: World) -> None:
    await partner_turned_it_off(world)
    await send(world.alice, photo=PHOTO)
    send_button = world.alice.session.last_buttons()[rl.SEND_BUTTON]
    await world.alice.send(END_CHAT)
    await match(world)  # a new chat (with Bob again, as nobody else is waiting)
    await world.alice.press_data(send_button)
    assert world.alice.session.callback_answers()[-1] == rl.HELD_EXPIRED
    assert to_bob(world) == 0
