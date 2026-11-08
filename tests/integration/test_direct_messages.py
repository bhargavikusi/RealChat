"""Direct messages (D-057): preview, Send, delivery and charging, Open, and the free
reply."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import Chat as TelegramChat
from aiogram.types import Dice, Message, Update
from aiogram.types import User as TelegramUser
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import direct as dm
from apps.bot.endings import BLOCK_BUTTON, LIKE_BUTTON, REPORT_BUTTON, DirectAction
from apps.bot.keyboards import END_CHAT, PARTNER_PROFILE
from apps.bot.relay import BLOCKED
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat.content import Blocked
from backend.modules.chat.retention import ChatRetention
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinHold, CoinTransaction
from backend.modules.direct_messages import service as direct
from backend.modules.direct_messages.models import DirectMessage
from backend.modules.direct_messages.service import Content, Refusal
from backend.modules.users import service as users
from backend.modules.users.models import User
from backend.modules.users.service import Notification
from tests.support.chat import USER_ID, Chat
from tests.support.db import wait_until_blocked
from tests.support.world import (
    BOB_ID,
    MESSAGE,
    REQUEST,
    World,
    buttons,
    last_to,
    match,
    onboarded_world,
    own_buttons,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
ALICE = "Night Owl"
BOB = "Moon Child"
TEXT = Content("text", "hello there", None, None)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    """Alice and Bob, onboarded, who have never chatted. Each has the welcome bonus."""
    return await onboarded_world(bot_settings, redis, session_factory)


async def public_id(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await users.get_profile(session, user.id)).public_id


async def write_to(chat: Chat, sessions: Sessions, telegram_id: int) -> None:
    """Tap ✉️ Message on `telegram_id`'s card."""
    profile = await public_id(sessions, telegram_id)
    await chat.press_data(DirectAction(action="write", profile=profile).pack())


async def compose(chat: Chat, sessions: Sessions, telegram_id: int, text: str) -> None:
    await write_to(chat, sessions, telegram_id)
    await chat.send(text)


async def send(chat: Chat, sessions: Sessions, telegram_id: int, text: str = "hello") -> None:
    """Write, preview, and Send a text message to `telegram_id`."""
    await compose(chat, sessions, telegram_id, text)
    await chat.press_data(own_buttons(chat)[dm.SEND_BUTTON])


def answer(chat: Chat) -> str:
    return chat.session.callback_answers()[-1]


def own_text(chat: Chat) -> str:
    return text_of(last_to(chat.session, chat.user_id))


async def balance(sessions: Sessions, telegram_id: int) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await coins.balance(session, user.id)).available


async def dm_charges(sessions: Sessions) -> int:
    async with sessions() as session:
        found = await session.scalar(
            select(func.count())
            .select_from(CoinTransaction)
            .where(CoinTransaction.kind == "direct_message")
        )
        return int(found or 0)


async def messages(sessions: Sessions) -> list[DirectMessage]:
    async with sessions() as session:
        return list(await session.scalars(select(DirectMessage).order_by(DirectMessage.id)))


async def hold_statuses(sessions: Sessions) -> list[str]:
    async with sessions() as session:
        rows = await session.scalars(
            select(CoinHold.status).where(CoinHold.purpose == "direct_message")
        )
        return list(rows)


def notices(chat: Chat, telegram_id: int, prefix: str = "✉️ Message from") -> list[str]:
    return [text_of(m) for m in chat.session.sent_to(telegram_id) if text_of(m).startswith(prefix)]


def notice_open(chat: Chat) -> str:
    """The Open button of the latest notice `chat`'s bot sent."""
    return chat.session.last_buttons()[dm.OPEN_BUTTON]


async def set_setting(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        session.add(AppSetting(key=key.value, value=value))
        await session.commit()


def refuse_notices(chat: Chat, error: type[Exception], description: str) -> None:
    """Make `chat`'s bot fail to send any direct message notice."""
    telegram = chat.session
    record = telegram.make_request

    async def refusing(bot: Any, method: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage) and dm.OPEN_BUTTON in str(method.reply_markup):
            telegram.requests.append(method)
            raise error(method=method, message=description)  # type: ignore[call-arg]
        return await record(bot, method, timeout)

    telegram.make_request = refusing  # type: ignore[method-assign]


# --- Writing and the preview ---------------------------------------------------------


async def test_a_message_is_previewed_before_sending(
    world: World, session_factory: Sessions
) -> None:
    await write_to(world.alice, session_factory, BOB_ID)
    assert own_text(world.alice) == dm.WRITE_PROMPT.format(nickname=BOB, price="1 coin")
    await world.alice.send("hello there")
    preview_header, preview = world.alice.session.sent_to(USER_ID)[-2:]
    assert text_of(preview_header) == dm.PREVIEW.format(nickname=BOB)
    assert text_of(preview) == "hello there"
    assert isinstance(preview, SendMessage) and preview.protect_content is True
    assert buttons(preview) == [dm.SEND_BUTTON, dm.CANCEL_BUTTON]
    # Nothing is sent or charged until Send.
    assert await messages(session_factory) == []
    assert notices(world.alice, BOB_ID) == []


async def test_a_new_message_replaces_the_preview(world: World, session_factory: Sessions) -> None:
    await compose(world.alice, session_factory, BOB_ID, "first")
    old_send = own_buttons(world.alice)[dm.SEND_BUTTON]
    await world.alice.send("second")
    new_send = own_buttons(world.alice)[dm.SEND_BUTTON]
    await world.alice.press_data(old_send)
    assert answer(world.alice) == dm.STALE_PREVIEW
    await world.alice.press_data(new_send)
    (sent,) = await messages(session_factory)
    assert sent.text == "second"


async def test_cancel_sends_nothing(world: World, session_factory: Sessions) -> None:
    await compose(world.alice, session_factory, BOB_ID, "never mind")
    send_data = own_buttons(world.alice)[dm.SEND_BUTTON]
    await world.alice.press_data(own_buttons(world.alice)[dm.CANCEL_BUTTON])
    assert answer(world.alice) == dm.CANCELLED
    await world.alice.press_data(send_data)
    assert answer(world.alice) == dm.STALE_PREVIEW
    # The next message is no longer taken as a direct message.
    previews = world.alice.session.sent_texts().count(dm.PREVIEW.format(nickname=BOB))
    await world.alice.send("just chatting")
    assert world.alice.session.sent_texts().count(dm.PREVIEW.format(nickname=BOB)) == previews
    assert await messages(session_factory) == []


async def test_messages_follow_the_chat_rules(world: World, session_factory: Sessions) -> None:
    await compose(world.alice, session_factory, BOB_ID, "visit https://example.com")
    assert own_text(world.alice) == BLOCKED[Blocked.LINK]
    assert dm.PREVIEW.format(nickname=BOB) not in world.alice.session.sent_texts()


async def test_dice_are_refused(world: World, session_factory: Sessions) -> None:
    await write_to(world.alice, session_factory, BOB_ID)
    dice = Update(
        update_id=990_001,
        message=Message(
            message_id=990_001,
            date=datetime.now(UTC),
            chat=TelegramChat(id=USER_ID, type="private"),
            from_user=TelegramUser(id=USER_ID, is_bot=False, first_name="Test"),
            dice=Dice(emoji="🎲", value=3),
        ),
    )
    await world.alice.dispatcher.feed_update(world.alice.bot, dice)
    assert own_text(world.alice) == dm.UNSUPPORTED


async def test_a_photo_is_previewed_and_opened_as_a_photo(
    world: World, session_factory: Sessions
) -> None:
    await write_to(world.alice, session_factory, BOB_ID)
    await world.alice.send_photo("photo-file")
    preview = world.alice.session.sent_photos()[-1]
    assert preview.photo == "photo-file" and preview.protect_content is True
    await world.alice.press_data(own_buttons(world.alice)[dm.SEND_BUTTON])
    await world.bob.press_data(notice_open(world.alice))
    opened = world.bob.session.sent_photos()[-1]
    assert opened.photo == "photo-file" and opened.protect_content is True


# --- Sending, delivery, and charging ---------------------------------------------------


async def test_sending_delivers_a_notice_and_charges_once(
    world: World, session_factory: Sessions
) -> None:
    before = await balance(session_factory, USER_ID)
    await send(world.alice, session_factory, BOB_ID)
    alice_id = await public_id(session_factory, USER_ID)
    notice = last_to(world.alice.session, BOB_ID)
    assert text_of(notice) == f"✉️ Message from {ALICE} · /userid_{alice_id}"
    assert buttons(notice) == [dm.OPEN_BUTTON]
    assert own_text(world.alice) == dm.DELIVERED.format(nickname=BOB, price="1 coin")
    assert await balance(session_factory, USER_ID) == before - 1
    assert await dm_charges(session_factory) == 1
    (sent,) = await messages(session_factory)
    assert (sent.status, sent.price, sent.failure_reason) == ("delivered", 1, None)
    assert await hold_statuses(session_factory) == ["captured"]


async def test_a_double_tap_on_send_sends_and_charges_once(
    world: World, session_factory: Sessions
) -> None:
    await compose(world.alice, session_factory, BOB_ID, "hello")
    data = own_buttons(world.alice)[dm.SEND_BUTTON]
    await world.alice.press_data(data)
    await world.alice.press_data(data)
    assert answer(world.alice) == dm.ALREADY_SENT
    assert len(notices(world.alice, BOB_ID)) == 1
    assert await dm_charges(session_factory) == 1


async def test_concurrent_sends_of_one_draft_record_one_message_and_one_hold(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as first, session_factory() as second:
        one = await direct.send(first, alice, bob, TEXT, "draft", 1)
        other = asyncio.create_task(direct.send(second, alice, bob, TEXT, "draft", 1))
        await wait_until_blocked(session_factory)
        await first.commit()
        two = await other
        await second.commit()
    assert one.message_id is not None and not one.already
    assert two.already and two.message_id == one.message_id
    assert len(await messages(session_factory)) == 1
    assert await hold_statuses(session_factory) == ["held"]


async def test_each_message_is_charged_separately(world: World, session_factory: Sessions) -> None:
    before = await balance(session_factory, USER_ID)
    await send(world.alice, session_factory, BOB_ID, "one")
    await send(world.alice, session_factory, BOB_ID, "two")
    assert await balance(session_factory, USER_ID) == before - 2
    assert len(notices(world.alice, BOB_ID)) == 2


async def test_the_price_is_an_admin_setting(world: World, session_factory: Sessions) -> None:
    await set_setting(session_factory, SettingKey.DIRECT_MESSAGE_COINS, 3)
    before = await balance(session_factory, USER_ID)
    await write_to(world.alice, session_factory, BOB_ID)
    assert own_text(world.alice) == dm.WRITE_PROMPT.format(nickname=BOB, price="3 coins")
    await world.alice.send("hi")
    await world.alice.press_data(own_buttons(world.alice)[dm.SEND_BUTTON])
    assert await balance(session_factory, USER_ID) == before - 3


async def test_the_price_shown_when_writing_is_the_price_charged(
    world: World, session_factory: Sessions
) -> None:
    before = await balance(session_factory, USER_ID)
    await compose(world.alice, session_factory, BOB_ID, "hello")
    await set_setting(session_factory, SettingKey.DIRECT_MESSAGE_COINS, 5)
    # As an admin's save does, so the bot reads the new price at once.
    world.alice.dispatcher["runtime_settings"].invalidate()
    await world.alice.press_data(own_buttons(world.alice)[dm.SEND_BUTTON])
    assert own_text(world.alice) == dm.DELIVERED.format(nickname=BOB, price="1 coin")
    assert await balance(session_factory, USER_ID) == before - 1


async def test_not_enough_coins_is_refused_before_writing(
    world: World, session_factory: Sessions
) -> None:
    await set_setting(session_factory, SettingKey.DIRECT_MESSAGE_COINS, 11)
    await write_to(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == dm.NO_COINS.format(price="11 coins", available="10 coins")
    await world.alice.send("hello")
    assert dm.PREVIEW.format(nickname=BOB) not in world.alice.session.sent_texts()
    assert await messages(session_factory) == []


async def test_coins_spent_meanwhile_refuse_the_send(
    world: World, session_factory: Sessions
) -> None:
    await compose(world.alice, session_factory, BOB_ID, "hello")
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        await coins.debit(session, alice, 10, "admin_correction", "drain", note="test")
        await session.commit()
    await world.alice.press_data(own_buttons(world.alice)[dm.SEND_BUTTON])
    assert answer(world.alice) == dm.REFUSALS[Refusal.NO_COINS]
    assert await messages(session_factory) == []
    assert notices(world.alice, BOB_ID) == []


@pytest.mark.parametrize(
    ("error", "description", "reason"),
    [
        (TelegramForbiddenError, "Forbidden: bot was blocked by the user", "unreachable"),
        (TelegramBadRequest, "Bad Request: something else", "error"),
    ],
)
async def test_a_failed_notice_releases_the_coin_and_records_why(
    world: World,
    session_factory: Sessions,
    error: type[Exception],
    description: str,
    reason: str,
) -> None:
    before = await balance(session_factory, USER_ID)
    refuse_notices(world.alice, error, description)
    await send(world.alice, session_factory, BOB_ID)
    assert own_text(world.alice) == dm.NOT_DELIVERED.format(nickname=BOB)
    assert await balance(session_factory, USER_ID) == before
    assert await dm_charges(session_factory) == 0
    (sent,) = await messages(session_factory)
    assert (sent.status, sent.failure_reason) == ("failed", reason)
    assert await hold_statuses(session_factory) == ["released"]
    # A failed message never opens.
    await world.bob.press_data(DirectAction(action="open", message=sent.id).pack())
    assert answer(world.bob) == dm.REFUSALS[Refusal.EXPIRED]


async def test_an_interrupted_send_is_failed_by_the_worker_without_a_charge(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        sent = await direct.send(session, alice, bob, TEXT, "draft", 1)
        await session.commit()
    assert sent.message_id is not None
    sweep = direct.PendingSweep(session_factory)
    now = datetime.now(UTC)
    assert await sweep.tick(now) == 0  # too recent
    assert await sweep.tick(now + direct.STALE_AFTER + timedelta(seconds=5)) == 1
    (row,) = await messages(session_factory)
    assert (row.status, row.failure_reason) == ("failed", "interrupted")
    assert await hold_statuses(session_factory) == ["released"]
    # A notice that arrives after the sweep is never charged.
    async with session_factory() as session:
        assert not await direct.delivered(session, sent.message_id, now)
        await session.commit()
    assert await dm_charges(session_factory) == 0


async def test_silent_mode_does_not_stop_direct_messages(
    world: World, session_factory: Sessions
) -> None:
    bob = await world.user_id(BOB_ID)
    async with session_factory() as session:
        await users.set_notification(session, bob, Notification.SILENT_MODE, True)
        await session.commit()
    await send(world.alice, session_factory, BOB_ID)
    assert len(notices(world.alice, BOB_ID)) == 1


# --- Who can be messaged ---------------------------------------------------------------


async def test_cards_offer_message_except_to_the_current_partner(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    assert buttons(last_to(world.alice.session, USER_ID)) == [
        LIKE_BUTTON,
        MESSAGE,
        REQUEST,
        BLOCK_BUTTON,
        REPORT_BUTTON,
    ]
    await match(world)
    await world.alice.send(PARTNER_PROFILE)
    assert MESSAGE not in buttons(last_to(world.alice.session, USER_ID))
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    assert MESSAGE not in buttons(last_to(world.alice.session, USER_ID))
    await write_to(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == dm.REFUSALS[Refusal.IN_CHAT]
    await world.alice.send(END_CHAT)
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    assert MESSAGE in buttons(last_to(world.alice.session, USER_ID))


async def test_someone_blocked_sees_no_message_button_and_cannot_send(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    await world.bob.send(f"/userid_{await public_id(session_factory, USER_ID)}")
    await world.bob.press_data(own_buttons(world.bob)[BLOCK_BUTTON])
    # Bob blocked Alice: Bob's card shows Alice no Message button.
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    assert MESSAGE not in buttons(last_to(world.alice.session, USER_ID))
    await write_to(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == dm.REFUSALS[Refusal.BLOCKED]
    async with session_factory() as session:
        sent = await direct.send(session, alice, bob, TEXT, "forged", 1)
        await session.commit()
    assert sent.refusal == Refusal.BLOCKED
    assert await messages(session_factory) == [] and await hold_statuses(session_factory) == []


async def test_the_blocker_must_unblock_first(world: World, session_factory: Sessions) -> None:
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    assert MESSAGE in buttons(last_to(world.alice.session, USER_ID))
    await write_to(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == dm.REFUSALS[Refusal.YOU_BLOCKED]


async def test_a_block_after_the_preview_refuses_the_send_without_a_charge(
    world: World, session_factory: Sessions
) -> None:
    before = await balance(session_factory, USER_ID)
    await compose(world.alice, session_factory, BOB_ID, "hello")
    send_data = own_buttons(world.alice)[dm.SEND_BUTTON]
    await world.bob.send(f"/userid_{await public_id(session_factory, USER_ID)}")
    await world.bob.press_data(own_buttons(world.bob)[BLOCK_BUTTON])
    await world.alice.press_data(send_data)
    assert answer(world.alice) == dm.REFUSALS[Refusal.BLOCKED]
    assert await balance(session_factory, USER_ID) == before
    assert await messages(session_factory) == [] and await hold_statuses(session_factory) == []


async def test_unavailable_accounts_cannot_be_messaged(
    world: World, session_factory: Sessions
) -> None:
    profile = await public_id(session_factory, BOB_ID)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == BOB_ID).values(status="suspended")
        )
        await session.commit()
    await world.alice.press_data(DirectAction(action="write", profile=profile).pack())
    assert answer(world.alice) == dm.REFUSALS[Refusal.UNAVAILABLE]
    await world.alice.press_data(DirectAction(action="write", profile="zzzzzzz").pack())
    assert answer(world.alice) == dm.REFUSALS[Refusal.UNAVAILABLE]


async def test_you_cannot_message_yourself(world: World, session_factory: Sessions) -> None:
    await write_to(world.alice, session_factory, USER_ID)
    assert answer(world.alice) == dm.REFUSALS[Refusal.UNAVAILABLE]


# --- Opening and the free reply --------------------------------------------------------


async def test_opening_shows_the_message_and_tells_the_sender_once(
    world: World, session_factory: Sessions
) -> None:
    await send(world.alice, session_factory, BOB_ID, "hello there")
    open_data = notice_open(world.alice)
    await world.bob.press_data(open_data)
    opened = last_to(world.bob.session, BOB_ID)
    assert text_of(opened) == "hello there"
    assert isinstance(opened, SendMessage) and opened.protect_content is True
    assert buttons(opened) == [dm.REPLY_BUTTON]
    assert text_of(last_to(world.bob.session, USER_ID)) == dm.VIEWED.format(nickname=BOB)
    await world.bob.press_data(open_data)
    viewed = [m for m in world.bob.session.sent_to(USER_ID) if "opened your" in text_of(m)]
    assert len(viewed) == 1
    (row,) = await messages(session_factory)
    assert row.status == "viewed" and row.viewed_at is not None


async def test_only_the_recipient_can_open_a_message(
    world: World, session_factory: Sessions
) -> None:
    await send(world.alice, session_factory, BOB_ID)
    await world.alice.press_data(notice_open(world.alice))
    assert answer(world.alice) == dm.REFUSALS[Refusal.EXPIRED]


async def test_the_message_opens_even_if_the_sender_deleted_their_copy(
    world: World, session_factory: Sessions
) -> None:
    # The content is stored: Open never copies from the sender's chat.
    await send(world.alice, session_factory, BOB_ID, "kept")
    await world.bob.press_data(notice_open(world.alice))
    assert text_of(last_to(world.bob.session, BOB_ID)) == "kept"
    assert world.bob.session.copies() == []


async def test_one_free_reply_delivered_the_same_way(
    world: World, session_factory: Sessions
) -> None:
    await send(world.alice, session_factory, BOB_ID, "hello")
    await world.bob.press_data(notice_open(world.alice))
    reply_data = own_buttons(world.bob)[dm.REPLY_BUTTON]
    bob_before = await balance(session_factory, BOB_ID)
    await world.bob.press_data(reply_data)
    assert own_text(world.bob) == dm.REPLY_PROMPT.format(nickname=ALICE)
    await world.bob.send("hi back")
    await world.bob.press_data(own_buttons(world.bob)[dm.SEND_BUTTON])
    bob_id = await public_id(session_factory, BOB_ID)
    notice = last_to(world.bob.session, USER_ID)
    assert text_of(notice) == f"↩️ Reply from {BOB} · /userid_{bob_id}"
    assert own_text(world.bob) == dm.REPLY_DELIVERED.format(nickname=ALICE)
    assert await balance(session_factory, BOB_ID) == bob_before
    assert await dm_charges(session_factory) == 1
    # The reply opens without a free reply of its own.
    await world.alice.press_data(world.bob.session.last_buttons()[dm.OPEN_BUTTON])
    opened = last_to(world.alice.session, USER_ID)
    assert text_of(opened) == "hi back" and buttons(opened) == []
    # And the paid message's free reply is used up.
    await world.bob.press_data(reply_data)
    assert answer(world.bob) == dm.REFUSALS[Refusal.REPLIED]
    await world.bob.press_data(notice_open(world.alice))
    assert buttons(last_to(world.bob.session, BOB_ID)) == []


async def test_concurrent_free_replies_send_only_one(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    since = datetime.now(UTC) - timedelta(days=7)
    async with session_factory() as session:
        sent = await direct.send(session, alice, bob, TEXT, "paid", 1)
        assert sent.message_id is not None
        await direct.delivered(session, sent.message_id, datetime.now(UTC))
        await session.commit()
    original = sent.message_id
    async with session_factory() as first, session_factory() as second:
        one = await direct.send(first, bob, alice, TEXT, "r1", 1, reply_to=original, since=since)
        other = asyncio.create_task(
            direct.send(second, bob, alice, TEXT, "r2", 1, reply_to=original, since=since)
        )
        await wait_until_blocked(session_factory)
        await first.commit()
        two = await other
        await second.commit()
    assert one.message_id is not None and one.refusal is None
    assert two.message_id is None and two.refusal == Refusal.REPLIED
    replies = [m for m in await messages(session_factory) if m.reply_to_id == original]
    assert len(replies) == 1 and replies[0].price == 0 and replies[0].hold_id is None


async def test_no_free_reply_once_either_user_blocked_the_other(
    world: World, session_factory: Sessions
) -> None:
    await send(world.alice, session_factory, BOB_ID)
    open_data = notice_open(world.alice)
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    # Alice blocked Bob after sending. The message still opens, without a free reply.
    await world.bob.press_data(open_data)
    opened = last_to(world.bob.session, BOB_ID)
    assert text_of(opened) == "hello" and buttons(opened) == []
    (row,) = await messages(session_factory)
    await world.bob.press_data(DirectAction(action="reply", message=row.id).pack())
    assert answer(world.bob) == dm.REFUSALS[Refusal.BLOCKED]


async def test_messages_expire_with_the_chat_retention_period(
    world: World, session_factory: Sessions, redis: Redis
) -> None:
    await send(world.alice, session_factory, BOB_ID)
    open_data = notice_open(world.alice)
    (row,) = await messages(session_factory)
    async with session_factory() as session:
        await session.execute(
            update(DirectMessage).values(created_at=DirectMessage.created_at - timedelta(days=8))
        )
        await session.commit()
    await world.bob.press_data(open_data)
    assert answer(world.bob) == dm.REFUSALS[Refusal.EXPIRED]
    await world.bob.press_data(DirectAction(action="reply", message=row.id).pack())
    assert answer(world.bob) == dm.REFUSALS[Refusal.EXPIRED]
    # The daily cleanup deletes it; the charge stays in the ledger.
    retention = ChatRetention(redis, session_factory, RuntimeSettings(ttl_seconds=0))
    assert await retention.run(datetime.now(UTC)) == 1
    assert await messages(session_factory) == []
    assert await dm_charges(session_factory) == 1


async def test_deleting_an_account_erases_its_messages_both_ways(
    world: World, session_factory: Sessions
) -> None:
    await send(world.alice, session_factory, BOB_ID)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        pending = await direct.send(session, bob, alice, TEXT, "pending", 1)
        await session.commit()
    assert pending.message_id is not None
    async with session_factory() as session:
        await users.delete_account(session, bob, datetime.now(UTC))
        await session.commit()
    assert await messages(session_factory) == []
    assert sorted(await hold_statuses(session_factory)) == ["captured", "released"]


async def test_a_send_waits_for_an_account_deletion_in_progress(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as deleting, session_factory() as sending:
        await users.delete_account(deleting, bob, datetime.now(UTC))
        pending = asyncio.create_task(direct.send(sending, alice, bob, TEXT, "draft", 1))
        await wait_until_blocked(session_factory)
        await deleting.commit()
        sent = await pending
        await sending.commit()
    assert sent.refusal == Refusal.UNAVAILABLE
    assert await messages(session_factory) == [] and await hold_statuses(session_factory) == []


async def test_deleting_the_recipient_releases_the_senders_pending_coin(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        pending = await direct.send(session, alice, bob, TEXT, "pending", 1)
        await session.commit()
    assert pending.message_id is not None
    async with session_factory() as session:
        await users.delete_account(session, bob, datetime.now(UTC))
        await session.commit()
    assert await hold_statuses(session_factory) == ["released"]
    assert await balance(session_factory, USER_ID) == 10


async def test_a_delivery_waits_for_the_senders_account_deletion(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        pending = await direct.send(session, alice, bob, TEXT, "pending", 1)
        await session.commit()
    assert pending.message_id is not None
    async with session_factory() as deleting, session_factory() as delivering:
        # The deletion holds the sender's account, as it does first, before reaching
        # the messages; the delivery must wait there rather than lock the message and
        # then wait for the account (a deadlock once the deletion reaches the message).
        await users.lock_user(deleting, alice)
        settled = asyncio.create_task(
            direct.delivered(delivering, pending.message_id, datetime.now(UTC))
        )
        await wait_until_blocked(session_factory)
        await users.delete_account(deleting, alice, datetime.now(UTC))
        await deleting.commit()
        assert not await settled
        await delivering.commit()
    assert await dm_charges(session_factory) == 0
    assert await messages(session_factory) == []


@pytest.mark.parametrize(
    ("kind", "text", "file", "status", "delivered", "viewed", "constraint"),
    [
        ("text", "hi", "file", "delivered", True, False, "content_file"),
        ("photo", None, None, "delivered", True, False, "content_file"),
        ("text", None, None, "delivered", True, False, "content_text"),
        ("text", "hi", None, "delivered", False, False, "delivered_pair"),
        ("text", "hi", None, "pending", True, False, "delivered_pair"),
        ("text", "hi", None, "delivered", True, True, "viewed_pair"),
        ("text", "hi", None, "viewed", True, False, "viewed_pair"),
    ],
)
async def test_the_database_keeps_messages_consistent(
    world: World,
    session_factory: Sessions,
    kind: str,
    text: str | None,
    file: str | None,
    status: str,
    delivered: bool,
    viewed: bool,
    constraint: str,
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    statement = sql(
        "INSERT INTO direct_messages (sender_user_id, recipient_user_id, idempotency_key, "
        "price, kind, text, file_id, status, delivered_at, viewed_at) "
        "VALUES (:a, :b, 'k', 0, :kind, :text, :file, :status, "
        "CASE WHEN :delivered THEN now() END, CASE WHEN :viewed THEN now() END)"
    )
    values = {"kind": kind, "text": text, "file": file, "status": status}
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match=f"ck_direct_messages_{constraint}"):
            await session.execute(
                statement,
                {"a": alice, "b": bob, **values, "delivered": delivered, "viewed": viewed},
            )


async def test_a_hold_pays_for_one_message(world: World, session_factory: Sessions) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        sent = await direct.send(session, alice, bob, TEXT, "first", 1)
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match="uq_direct_messages_hold_id"):
            await session.execute(
                sql(
                    "INSERT INTO direct_messages (sender_user_id, recipient_user_id, "
                    "idempotency_key, price, hold_id, kind, text) "
                    "SELECT :a, :b, 'second', 1, hold_id, 'text', 'hi' "
                    "FROM direct_messages WHERE id = :m"
                ),
                {"a": alice, "b": bob, "m": sent.message_id},
            )
