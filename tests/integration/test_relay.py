"""Relaying messages between matched users (D-028, D-036, D-038, D-042, D-043)."""

import logging
from itertools import count
from typing import Any

import pytest
from aiogram.methods import CopyMessage, DeleteMessage, EditMessageCaption, EditMessageText
from aiogram.types import Chat as TelegramChat
from aiogram.types import (
    Contact,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Location,
    Message,
    MessageEntity,
    PhotoSize,
    Sticker,
    User,
    Venue,
    Voice,
)
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import onboarding as ob
from apps.bot import profile as pr
from apps.bot import relay as rl
from apps.bot.endings import PARTNER_ENDED, PARTNER_UNREACHABLE
from apps.bot.keyboards import END_CHAT
from backend.config.settings import BotSettings
from backend.modules.chat import service as chat_service
from backend.modules.chat.content import Blocked
from backend.modules.chat.models import ChatMessage, Conversation
from backend.modules.users.models import TermsAcceptance
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding
from tests.support.telegram import content_update, edited_update
from tests.support.world import (
    BOB_ID,
    World,
    assert_chat_ended,
    match,
    onboarded_world,
    search,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
_message_ids = count(50_000)
# 9876543210 in full-width digits, which look like digits but are different characters.
FULL_WIDTH_NUMBER = "".join(chr(0xFF10 + int(digit)) for digit in "9876543210")


def full_width(text: str) -> str:
    """ASCII letters and punctuation as their full-width look-alikes."""
    return "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in text)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    world = await onboarded_world(bot_settings, redis, session_factory)
    await match(world)
    return world


async def send(chat: Chat, **fields: Any) -> int:
    """Send a message with any content; returns its message id."""
    message_id = next(_message_ids)
    await chat.dispatcher.feed_update(
        chat.bot, content_update(next(Chat._ids), chat.user_id, message_id, **fields)
    )
    return message_id


async def edit(chat: Chat, message_id: int, **fields: Any) -> None:
    await chat.dispatcher.feed_update(
        chat.bot, edited_update(next(Chat._ids), chat.user_id, message_id, **fields)
    )


def replying_to(user_id: int, message_id: int) -> Message:
    return Message(
        message_id=message_id,
        date=0,  # type: ignore[arg-type]
        chat=TelegramChat(id=user_id, type="private"),
        text="-",
    )


async def stored(sessions: Sessions) -> list[ChatMessage]:
    async with sessions() as session:
        return list((await session.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars())


def copies(chat: Chat) -> list[CopyMessage]:
    return chat.session.copies()


# --- Relaying ------------------------------------------------------------------------


async def test_text_reaches_only_the_partner_protected(
    world: World, session_factory: Sessions
) -> None:
    message_id = await send(world.alice, text="Hello there")
    [copy] = copies(world.alice)
    assert (copy.chat_id, copy.from_chat_id, copy.message_id) == (BOB_ID, USER_ID, message_id)
    assert copy.protect_content is True
    [row] = await stored(session_factory)
    assert (row.kind, row.text, row.file_id, row.sender_message_id) == (
        "text",
        "Hello there",
        None,
        message_id,
    )


async def test_both_directions_relay(world: World) -> None:
    await send(world.alice, text="Hi")
    await send(world.bob, text="Hey")
    assert [c.chat_id for c in copies(world.alice)] == [BOB_ID]
    assert [c.chat_id for c in copies(world.bob)] == [USER_ID]


@pytest.mark.parametrize(
    ("fields", "kind", "file_id"),
    [
        (
            {
                "photo": [
                    PhotoSize(file_id="small", file_unique_id="s", width=90, height=90),
                    PhotoSize(file_id="large", file_unique_id="l", width=800, height=800),
                ],
                "caption": "Sunset",
            },
            "photo",
            "large",
        ),
        (
            {
                "sticker": Sticker(
                    file_id="stk",
                    file_unique_id="u",
                    type="regular",
                    width=512,
                    height=512,
                    is_animated=False,
                    is_video=False,
                )
            },
            "sticker",
            "stk",
        ),
        ({"voice": Voice(file_id="vo", file_unique_id="u", duration=3)}, "voice", "vo"),
        ({"document": Document(file_id="doc", file_unique_id="u")}, "document", "doc"),
    ],
)
async def test_media_is_relayed_and_stored_by_file_identifier(
    world: World, session_factory: Sessions, fields: dict[str, Any], kind: str, file_id: str
) -> None:
    await send(world.alice, **fields)
    assert len(copies(world.alice)) == 1
    [row] = await stored(session_factory)
    assert (row.kind, row.file_id, row.text) == (kind, file_id, fields.get("caption"))


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"contact": Contact(phone_number="+15551234567", first_name="A")}, Blocked.CONTACT),
        ({"location": Location(latitude=52.52, longitude=13.4)}, Blocked.LOCATION),
        (
            {
                "venue": Venue(
                    location=Location(latitude=52.52, longitude=13.4),
                    title="Cafe",
                    address="Street 1",
                ),
                "location": Location(latitude=52.52, longitude=13.4),
            },
            Blocked.LOCATION,
        ),
        ({"text": "see example.com"}, Blocked.LINK),
        ({"text": "https://t.me/someone"}, Blocked.LINK),
        ({"text": "call me +1 (555) 123-4567"}, Blocked.PHONE),
        ({"text": FULL_WIDTH_NUMBER}, Blocked.PHONE),
        ({"text": f"see {full_width('example.com')}"}, Blocked.LINK),
        ({"text": f"I'm {full_width('@night_owl_99')}"}, Blocked.USERNAME),
        ({"text": "exa\u200bmple.com"}, Blocked.LINK),  # an invisible zero-width space
        ({"text": "555 - 123 - 4567"}, Blocked.PHONE),
        ({"text": "see ex\u0430mple.com"}, Blocked.LINK),  # a Cyrillic small a
        ({"text": "see example.\u0441\u043em"}, Blocked.LINK),  # ".com" in Cyrillic letters
        ({"text": "example.\u034fcom"}, Blocked.LINK),  # a combining grapheme joiner
        ({"text": "I'm @\u034fnight_owl_99"}, Blocked.USERNAME),
        (
            {
                "text": "click here",
                "entities": [
                    MessageEntity(type="text_link", offset=0, length=5, url="https://x.test")
                ],
            },
            Blocked.LINK,
        ),
        (
            {
                "text": "ask him",
                "entities": [
                    MessageEntity(
                        type="text_mention",
                        offset=4,
                        length=3,
                        user=User(id=99, is_bot=False, first_name="Him"),
                    )
                ],
            },
            Blocked.USERNAME,
        ),
        (
            {
                "text": "a nice game",
                "reply_markup": InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="Play", url="https://x.test")]]
                ),
            },
            Blocked.LINK,
        ),
        ({"text": "I'm @night_owl_99"}, Blocked.USERNAME),
        (
            {
                "photo": [PhotoSize(file_id="p", file_unique_id="u", width=1, height=1)],
                "caption": "www.example.org",
            },
            Blocked.LINK,
        ),
    ],
)
async def test_identifying_content_is_refused(
    world: World, session_factory: Sessions, fields: dict[str, Any], reason: Blocked
) -> None:
    await send(world.alice, **fields)
    assert copies(world.alice) == []
    assert world.alice.last() == rl.BLOCKED[reason]
    assert await stored(session_factory) == []


@pytest.mark.parametrize("text", ["I'm 25 and live near the river", "Meet at 7:30?", "lol..."])
async def test_ordinary_text_is_relayed(world: World, text: str) -> None:
    await send(world.alice, text=text)
    assert len(copies(world.alice)) == 1


async def test_commands_and_buttons_are_not_relayed(world: World) -> None:
    await world.alice.send("/help")
    await world.alice.send(END_CHAT)
    assert copies(world.alice) == []


async def test_nothing_is_relayed_outside_a_chat(world: World) -> None:
    await world.alice.send(END_CHAT)
    await send(world.alice, text="still there?")
    assert copies(world.alice) == []
    assert world.alice.last() == ob.WELCOME_BACK


async def test_users_not_in_the_chat_reach_no_one(
    world: World, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    outsider = Chat(bot_settings, redis, session_factory, user_id=7171)
    await complete_onboarding(outsider)
    await send(outsider, text="hello?")
    assert copies(outsider) == []


async def test_a_pending_profile_edit_takes_the_answer(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.send("/profile")
    await world.alice.press(pr.BUTTON_LABELS[pr.ProfileField.NICKNAME])
    await send(world.alice, text="Star Gazer")
    assert copies(world.alice) == []
    assert await stored(session_factory) == []


async def test_new_terms_must_be_accepted_before_messages_are_stored(
    world: World, session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await session.execute(delete(TermsAcceptance))
        await session.commit()
    await send(world.alice, text="Hello")
    assert copies(world.alice) == []
    assert world.alice.last() == TERMS


# --- Replies and edits ---------------------------------------------------------------


async def test_replies_point_at_the_matching_message(world: World) -> None:
    original = await send(world.alice, text="How are you?")
    [copy] = copies(world.alice)
    copy_id = 10_000 + world.alice.session.requests.index(copy) + 1

    # Bob replies to the copy he received: Alice sees it as a reply to her original.
    await send(world.bob, text="Fine!", reply_to_message=replying_to(BOB_ID, copy_id))
    reply = copies(world.bob)[-1]
    assert reply.reply_parameters is not None
    assert reply.reply_parameters.message_id == original

    # Alice replies to her own message: Bob sees it as a reply to his copy.
    await send(world.alice, text="Great", reply_to_message=replying_to(USER_ID, original))
    own = copies(world.alice)[-1]
    assert own.reply_parameters is not None
    assert own.reply_parameters.message_id == copy_id


async def test_a_reply_to_an_unknown_message_is_sent_plainly(world: World) -> None:
    await send(world.alice, text="Hm", reply_to_message=replying_to(USER_ID, 1))
    assert copies(world.alice)[-1].reply_parameters is None


async def test_edits_are_mirrored_and_the_original_is_kept(
    world: World, session_factory: Sessions
) -> None:
    message_id = await send(world.alice, text="See you at 8")
    await edit(world.alice, message_id, text="See you at 9")
    [mirrored] = [r for r in world.alice.session.requests if isinstance(r, EditMessageText)]
    assert (mirrored.chat_id, mirrored.text) == (BOB_ID, "See you at 9")
    [row] = await stored(session_factory)
    assert (row.text, row.edited_text) == ("See you at 8", "See you at 9")
    assert row.edited_at is not None


async def test_caption_edits_are_mirrored(world: World) -> None:
    photo = [PhotoSize(file_id="p", file_unique_id="u", width=1, height=1)]
    message_id = await send(world.alice, photo=photo, caption="Old")
    await edit(world.alice, message_id, photo=photo, caption="New")
    [mirrored] = [r for r in world.alice.session.requests if isinstance(r, EditMessageCaption)]
    assert (mirrored.chat_id, mirrored.caption) == (BOB_ID, "New")


async def test_an_edit_adding_a_link_is_not_mirrored(
    world: World, session_factory: Sessions
) -> None:
    message_id = await send(world.alice, text="Hello")
    await edit(world.alice, message_id, text="Hello, see example.com")
    assert not any(isinstance(r, EditMessageText) for r in world.alice.session.requests)
    assert world.alice.last() == rl.EDIT_BLOCKED.format(reason=rl.BLOCKED[Blocked.LINK])
    [row] = await stored(session_factory)
    assert row.edited_text is None


async def test_edits_after_the_chat_ended_are_not_mirrored(world: World) -> None:
    message_id = await send(world.alice, text="Bye")
    await world.alice.send(END_CHAT)
    await edit(world.alice, message_id, text="Bye!")
    assert not any(isinstance(r, EditMessageText) for r in world.alice.session.requests)


# --- Duplicates and failures ---------------------------------------------------------


async def test_a_message_is_relayed_once(world: World, session_factory: Sessions) -> None:
    message_id = next(_message_ids)
    for _ in range(2):  # the same message delivered in two updates
        await world.alice.dispatcher.feed_update(
            world.alice.bot, content_update(next(Chat._ids), USER_ID, message_id, text="Once")
        )
    assert len(copies(world.alice)) == 1
    assert len(await stored(session_factory)) == 1


async def test_an_unreachable_partner_ends_the_chat(
    world: World, session_factory: Sessions
) -> None:
    world.alice.session.fail_copies("Forbidden: bot was blocked by the user", forbidden=True)
    await send(world.alice, text="Hello?")
    assert_chat_ended(world.alice.session, USER_ID, PARTNER_UNREACHABLE)
    async with session_factory() as session:
        conversation = (await session.execute(select(Conversation))).scalar_one()
        assert (conversation.end_reason, conversation.ended_by_user_id) == ("unreachable", None)
        bob = await world.user_id(BOB_ID)
        assert await chat_service.active_chat(session, bob) is None


async def test_unsupported_content_keeps_the_chat(world: World, session_factory: Sessions) -> None:
    world.alice.session.fail_copies("Bad Request: message can't be copied")
    await send(world.alice, text="something odd")
    assert world.alice.last() == rl.UNSUPPORTED
    async with session_factory() as session:
        assert await chat_service.active_chat(session, await world.user_id(USER_ID))


async def test_a_copy_delivered_after_the_chat_ended_is_withdrawn(
    world: World, session_factory: Sessions
) -> None:
    async def partner_ends_meanwhile() -> None:
        await world.bob.send(END_CHAT)

    world.alice.session.during_copy = partner_ends_meanwhile
    await send(world.alice, text="Are you there?")
    [copy] = copies(world.alice)
    copy_id = 10_000 + world.alice.session.requests.index(copy) + 1
    deletions = [r for r in world.alice.session.requests if isinstance(r, DeleteMessage)]
    assert [(d.chat_id, d.message_id) for d in deletions] == [(BOB_ID, copy_id)]
    assert await stored(session_factory) == []


async def test_an_unreachable_partner_who_already_ended_the_chat_is_not_announced_twice(
    world: World, session_factory: Sessions
) -> None:
    async def partner_ends_meanwhile() -> None:
        await world.bob.send(END_CHAT)

    world.alice.session.during_copy = partner_ends_meanwhile
    world.alice.session.fail_copies("Forbidden: bot was blocked by the user", forbidden=True)
    await send(world.alice, text="Are you there?")
    # Alice was told once, by Bob's ending, and not again as "can't be reached".
    assert_chat_ended(world.bob.session, USER_ID, PARTNER_ENDED)
    assert PARTNER_UNREACHABLE not in [text_of(m) for m in world.alice.session.sent_to(USER_ID)]
    async with session_factory() as session:
        conversation = (await session.execute(select(Conversation))).scalar_one()
        assert conversation.end_reason == "user_ended"


# --- Abuse and privacy ---------------------------------------------------------------


async def test_flooding_is_rate_limited(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    world = await onboarded_world(bot_settings, redis, session_factory)
    await match(world)
    limited = bot_settings.model_copy(update={"bot_flood_limit": 3})
    world.alice = Chat(limited, redis, session_factory)
    # Start from a clean window: onboarding and matching already counted updates.
    for key in await redis.keys("bot:flood:*"):
        await redis.delete(key)
    for n in range(6):
        await send(world.alice, text=f"spam {n}")
    assert len(copies(world.alice)) == 3


async def test_logs_never_contain_message_content(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world.alice.session.fail_copies("Bad Request: message can't be copied")
    await send(world.alice, text="my secret words")
    world.alice.session.copy_error = None
    message_id = await send(world.alice, text="more secret words")
    await edit(world.alice, message_id, text="edited secret words")
    for record in caplog.records:
        assert "secret" not in record.getMessage()
        assert "secret" not in str(record.__dict__)


async def test_an_old_chat_edit_never_reaches_a_new_partner(
    world: World, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    old = await send(world.alice, text="Hi Bob")
    await world.alice.send(END_CHAT)
    carol = Chat(bot_settings, redis, session_factory, user_id=8181)
    await complete_onboarding(carol)
    await search(world.alice)
    await search(carol)
    await world.tick()
    async with session_factory() as session:
        assert await chat_service.active_chat(session, await world.user_id(USER_ID))

    await edit(world.alice, old, text="Hi Bob!!")
    assert not any(isinstance(r, EditMessageText) for r in world.alice.session.requests)


# --- Review follow-ups ---------------------------------------------------------------


async def test_an_edit_adding_a_hidden_link_is_not_mirrored(world: World) -> None:
    message_id = await send(world.alice, text="click here")
    entity = MessageEntity(type="text_link", offset=0, length=5, url="https://x.test")
    await edit(world.alice, message_id, text="click here", entities=[entity])
    assert not any(isinstance(r, EditMessageText) for r in world.alice.session.requests)
    assert world.alice.last() == rl.EDIT_BLOCKED.format(reason=rl.BLOCKED[Blocked.LINK])


async def test_edits_wait_for_a_pending_answer(world: World) -> None:
    message_id = await send(world.alice, text="Hello")
    await world.alice.send("/profile")
    await world.alice.press(pr.BUTTON_LABELS[pr.ProfileField.BIO])
    await edit(world.alice, message_id, text="Hello again")
    assert not any(isinstance(r, EditMessageText) for r in world.alice.session.requests)


async def test_edits_need_the_current_terms(world: World, session_factory: Sessions) -> None:
    message_id = await send(world.alice, text="Hello")
    async with session_factory() as session:
        await session.execute(delete(TermsAcceptance))
        await session.commit()
    await edit(world.alice, message_id, text="Hello again")
    assert not any(isinstance(r, EditMessageText) for r in world.alice.session.requests)
    assert world.alice.last() == TERMS


async def test_failed_deliveries_leave_no_record(world: World, session_factory: Sessions) -> None:
    world.alice.session.fail_copies("Bad Request: message can't be copied")
    await send(world.alice, text="odd")
    assert await stored(session_factory) == []


async def test_a_message_is_never_relayed_twice_even_after_a_crash(
    world: World, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(*args: object, **kwargs: object) -> bool:
        raise ConnectionError("simulated crash after sending")

    monkeypatch.setattr(chat_service, "complete_message", crash)
    message_id = next(_message_ids)
    update = content_update(next(Chat._ids), USER_ID, message_id, text="Once only")
    await world.alice.dispatcher.feed_update(world.alice.bot, update)
    [copy] = copies(world.alice)
    copy_id = 10_000 + world.alice.session.requests.index(copy) + 1
    # The copy that could not be linked was taken back.
    deletions = [r for r in world.alice.session.requests if isinstance(r, DeleteMessage)]
    assert [(d.chat_id, d.message_id) for d in deletions] == [(BOB_ID, copy_id)]

    # Telegram delivers the same message again (a new update id): it is not resent.
    monkeypatch.undo()
    replies = len(world.alice.session.sent_texts())
    again = content_update(next(Chat._ids), USER_ID, message_id, text="Once only")
    await world.alice.dispatcher.feed_update(world.alice.bot, again)
    assert len(copies(world.alice)) == 1
    assert world.alice.session.sent_texts()[replies:] == []  # handled quietly, no error
    [row] = await stored(session_factory)
    assert row.recipient_message_id is None  # recorded, but never linked to a copy
