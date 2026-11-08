"""Direct messages: ✉️ Message on a profile card, and its free reply (D-057).

1. ✉️ Message on another user's card asks for the message and shows its price. The
   next message the user sends is checked against the chat rules (D-042) and shown back
   as a preview, exactly as the recipient will see it, with Send and Cancel. Sending
   another message instead replaces the preview.
2. Send holds the price and records the message (`backend.modules.direct_messages`),
   then tells the recipient who it is from (nickname and /userid link) with an Open
   button. Once Telegram accepts that notice the coin is taken and the sender is told;
   if it fails, the hold is released and the sender is told nothing was taken. Send is
   tied to its preview, so a repeated tap sends nothing more.
3. Open shows the stored message, protected from forwarding and saving, and tells the
   sender the first time. A paid message also offers ↩️ Reply (free) once, which works
   the same way at no cost; a free reply has no free reply of its own.

Block and Report are on the sender's card, which the notice's /userid link opens. The
wording and layout are provisional until the design pass. As elsewhere, every database
transaction is ended before any Telegram call.
"""

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.endings import DirectAction, coins_label
from apps.bot.people import NOT_AVAILABLE, ready_user
from apps.bot.relay import BLOCKED, content_refusal, file_id, message_kind, unreachable
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.coins import service as coins
from backend.modules.direct_messages import service as direct
from backend.modules.direct_messages.models import DIRECT_KINDS
from backend.modules.direct_messages.service import Content, Refusal
from backend.modules.users import service as users

logger = logging.getLogger(__name__)


class Compose(StatesGroup):
    message = State()  # waiting for the message
    preview = State()  # the preview is shown, waiting for Send


WRITE_PROMPT = (
    "✉️ Write your message to {nickname}. It costs {price}, taken only once it's delivered."
)
REPLY_PROMPT = "↩️ Write your free reply to {nickname}."
CANCEL_BUTTON = "Cancel"
SEND_BUTTON = "✅ Send"
PREVIEW = "👁 Preview: this is how {nickname} will see it."
UNSUPPORTED = "Polls, dice, and this kind of message can't be sent as a direct message."
CANCELLED = "Cancelled. Nothing was sent."
STALE_PREVIEW = "This preview isn't current any more."
ALREADY_SENT = "Already sent."
NO_COINS = "You need {price} to send a message, and you have {available}."
REFUSALS = {
    Refusal.UNAVAILABLE: NOT_AVAILABLE,
    Refusal.BLOCKED: "This person can't receive your messages.",
    Refusal.YOU_BLOCKED: "You blocked this person. Unblock them to message them.",
    Refusal.IN_CHAT: "You're chatting with this person now.",
    Refusal.NO_COINS: "You don't have enough coins to send this message.",
    Refusal.EXPIRED: "This message has expired.",
    Refusal.REPLIED: "You already replied to this message.",
}
MESSAGE_NOTICE = "✉️ Message from {nickname} · /userid_{public_id}"
REPLY_NOTICE = "↩️ Reply from {nickname} · /userid_{public_id}"
OPEN_BUTTON = "📩 Open"
REPLY_BUTTON = "↩️ Reply (free)"
DELIVERED = "✉️ Delivered to {nickname}. {price} used."
# Delivered, but settled meanwhile without a charge (e.g. by the worker's sweep).
DELIVERED_FREE = "✉️ Delivered to {nickname}."
REPLY_DELIVERED = "↩️ Your reply was delivered to {nickname}."
NOT_DELIVERED = "Your message couldn't be delivered to {nickname}. No coin was taken."
REPLY_NOT_DELIVERED = "Your reply couldn't be delivered to {nickname}."
VIEWED = "👀 {nickname} opened your message."


def _now() -> datetime:
    return datetime.now(UTC)


def _buttons(*rows: tuple[str, DirectAction]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data.pack())] for text, data in rows
        ]
    )


async def _retention_start(session: AsyncSession, runtime_settings: RuntimeSettings) -> datetime:
    days = await runtime_settings.get(session, SettingKey.CHAT_RETENTION_DAYS)
    return direct.retention_start(_now(), days)


async def send_content(
    bot: Bot, chat_id: int, content: Content, markup: InlineKeyboardMarkup | None
) -> None:
    """Send a direct message's stored content, protected from forwarding and saving."""
    entities = [MessageEntity.model_validate(e) for e in content.entities or ()] or None
    common: dict[str, Any] = {"reply_markup": markup, "protect_content": True}
    if content.kind == "text":
        await bot.send_message(chat_id, content.text or "", entities=entities, **common)
        return
    media = str(content.file_id)
    if content.kind == "video_note":
        await bot.send_video_note(chat_id, media, **common)
        return
    if content.kind == "sticker":
        await bot.send_sticker(chat_id, media, **common)
        return
    senders: dict[str, Callable[..., Awaitable[Any]]] = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "animation": bot.send_animation,
        "audio": bot.send_audio,
        "voice": bot.send_voice,
        "document": bot.send_document,
    }
    await senders[content.kind](
        chat_id, media, caption=content.text, caption_entities=entities, **common
    )


def _content(message: Message) -> Content:
    entities = message.entities or message.caption_entities
    return Content(
        kind=message_kind(message),
        text=message.text or message.caption,
        entities=[e.model_dump(mode="json", exclude_none=True) for e in entities]
        if entities
        else None,
        file_id=file_id(message),
    )


async def _remove_buttons(bot: Bot, query: CallbackQuery) -> None:
    """Best effort: Telegram may refuse, e.g. for an old message."""
    if query.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=None
        )
    except TelegramAPIError as exc:
        logger.info("could not remove buttons", extra=exception_summary(exc))


# --- Writing ---------------------------------------------------------------------------


async def on_write(
    query: CallbackQuery,
    bot: Bot,
    callback_data: DirectAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    other_id = await users.user_id_for(session, callback_data.profile)
    refused = (
        Refusal.UNAVAILABLE
        if other_id is None
        else await direct.refusal(session, user_id, other_id)
    )
    if refused is not None or other_id is None:
        await session.commit()
        await query.answer(REFUSALS[refused or Refusal.UNAVAILABLE])
        return
    price = await runtime_settings.get(session, SettingKey.DIRECT_MESSAGE_COINS)
    available = (await coins.balance(session, user_id)).available
    nickname = str((await users.get_profile(session, other_id)).nickname)
    await session.commit()
    if available < price:
        await query.answer(
            NO_COINS.format(price=coins_label(price), available=coins_label(available)),
            show_alert=True,
        )
        return
    await state.set_state(Compose.message)
    await state.set_data(
        {"recipient": other_id, "nickname": nickname, "reply_to": None, "price": price}
    )
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        WRITE_PROMPT.format(nickname=nickname, price=coins_label(price)),
        reply_markup=_buttons((CANCEL_BUTTON, DirectAction(action="cancel"))),
    )


async def on_reply(
    query: CallbackQuery,
    bot: Bot,
    callback_data: DirectAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    since = await _retention_start(session, runtime_settings)
    sender_id, refused = await direct.reply_refusal(session, callback_data.message, user_id, since)
    if refused is not None or sender_id is None:
        await session.commit()
        await query.answer(REFUSALS[refused or Refusal.EXPIRED])
        return
    nickname = str((await users.get_profile(session, sender_id)).nickname)
    await session.commit()
    await state.set_state(Compose.message)
    await state.set_data(
        {"recipient": sender_id, "nickname": nickname, "reply_to": callback_data.message}
    )
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        REPLY_PROMPT.format(nickname=nickname),
        reply_markup=_buttons((CANCEL_BUTTON, DirectAction(action="cancel"))),
    )


async def on_compose(message: Message, bot: Bot, state: FSMContext) -> None:
    """The message to send: checked, then shown back as a preview with Send."""
    refused = content_refusal(message)
    if refused is not None:
        await message.answer(BLOCKED[refused])
        return
    content = _content(message)
    if content.kind not in DIRECT_KINDS:
        await message.answer(UNSUPPORTED)
        return
    draft = secrets.token_hex(8)
    data = await state.get_data()
    await state.set_state(Compose.preview)
    await state.update_data(
        draft=draft,
        content={
            "kind": content.kind,
            "text": content.text,
            "entities": content.entities,
            "file_id": content.file_id,
        },
    )
    await message.answer(PREVIEW.format(nickname=data.get("nickname", "")))
    await send_content(
        bot,
        message.chat.id,
        content,
        _buttons(
            (SEND_BUTTON, DirectAction(action="send", draft=draft)),
            (CANCEL_BUTTON, DirectAction(action="cancel", draft=draft)),
        ),
    )


async def on_cancel(
    query: CallbackQuery, bot: Bot, callback_data: DirectAction, state: FSMContext
) -> None:
    data = await state.get_data()
    current = await state.get_state() in (Compose.message.state, Compose.preview.state)
    # A Cancel from an older preview leaves a newer one alone.
    if current and (not callback_data.draft or callback_data.draft == data.get("draft")):
        await state.clear()
    await query.answer(CANCELLED)
    await _remove_buttons(bot, query)


# --- Sending ---------------------------------------------------------------------------


async def on_send(
    query: CallbackQuery,
    bot: Bot,
    callback_data: DirectAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    key = f"{user_id}:{callback_data.draft}"
    data = await state.get_data()
    if await state.get_state() != Compose.preview.state or data.get("draft") != (
        callback_data.draft
    ):
        recorded = await direct.sent_under(session, key)
        await session.commit()
        await query.answer(ALREADY_SENT if recorded else STALE_PREVIEW)
        await _remove_buttons(bot, query)
        return
    recipient_id = int(data["recipient"])
    reply_to = data.get("reply_to")
    content = Content(**data["content"])
    # The price shown when writing began, so the user pays what they were told.
    price = int(data.get("price") or 0)
    since = await _retention_start(session, runtime_settings)
    sent = await direct.send(
        session,
        user_id,
        recipient_id,
        content,
        key,
        price,
        reply_to=None if reply_to is None else int(reply_to),
        since=since,
    )
    if sent.message_id is None or sent.already:
        await session.commit()
        await state.clear()
        refused = REFUSALS[sent.refusal or Refusal.UNAVAILABLE]
        await query.answer(ALREADY_SENT if sent.already else refused)
        await _remove_buttons(bot, query)
        return
    sender = await users.get_profile(session, user_id)
    recipient_chat = (await users.get_user(session, recipient_id)).telegram_user_id
    notice = (REPLY_NOTICE if reply_to is not None else MESSAGE_NOTICE).format(
        nickname=sender.nickname, public_id=sender.public_id
    )
    await session.commit()
    await state.clear()
    await query.answer()
    await _remove_buttons(bot, query)
    await _deliver(
        bot,
        session,
        query.from_user.id,
        recipient_chat,
        sent.message_id,
        notice,
        nickname=str(data.get("nickname", "")),
        price=0 if reply_to is not None else price,
    )


async def _deliver(
    bot: Bot,
    session: AsyncSession,
    sender_chat: int,
    recipient_chat: int,
    message_id: int,
    notice: str,
    *,
    nickname: str,
    price: int,
) -> None:
    """Send the recipient's notice, then settle the message by what Telegram said."""
    try:
        await bot.send_message(
            recipient_chat,
            notice,
            reply_markup=_buttons((OPEN_BUTTON, DirectAction(action="open", message=message_id))),
        )
    except TelegramAPIError as exc:
        logger.info("could not deliver a direct message", extra=exception_summary(exc))
        reason = "unreachable" if unreachable(exc) else "error"
        await direct.failed(session, message_id, reason, _now())
        await session.commit()
        text = NOT_DELIVERED if price else REPLY_NOT_DELIVERED
        await bot.send_message(sender_chat, text.format(nickname=nickname))
        return
    charged = await direct.delivered(session, message_id, _now())
    await session.commit()
    if not price:
        text = REPLY_DELIVERED.format(nickname=nickname)
    elif charged:
        text = DELIVERED.format(nickname=nickname, price=coins_label(price))
    else:
        text = DELIVERED_FREE.format(nickname=nickname)
    await bot.send_message(sender_chat, text)


# --- Opening ---------------------------------------------------------------------------


async def on_open(
    query: CallbackQuery,
    bot: Bot,
    callback_data: DirectAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    since = await _retention_start(session, runtime_settings)
    found = await direct.for_recipient(session, callback_data.message, user_id, since)
    if found is None:
        await session.commit()
        await query.answer(REFUSALS[Refusal.EXPIRED])
        return
    message_id, sender_id = found.id, found.sender_user_id
    content = Content(found.kind, found.text, found.entities, found.file_id)
    markup = None
    if found.price > 0:
        _, refused = await direct.reply_refusal(session, message_id, user_id, since)
        if refused is None:
            markup = _buttons((REPLY_BUTTON, DirectAction(action="reply", message=message_id)))
    await session.commit()
    await query.answer()
    try:
        await send_content(bot, query.from_user.id, content, markup)
    except TelegramAPIError as exc:
        logger.warning("could not show a direct message", extra=exception_summary(exc))
        return
    viewed = await direct.mark_viewed(session, message_id, _now())
    nickname = str((await users.get_profile(session, user_id)).nickname)
    sender_chat = (await users.get_user(session, sender_id)).telegram_user_id
    await session.commit()
    if not viewed:
        return
    try:
        await bot.send_message(sender_chat, VIEWED.format(nickname=nickname))
    except TelegramAPIError as exc:
        logger.info("could not send an opened notice", extra=exception_summary(exc))


def create_router() -> Router:
    router = Router(name="direct")
    # Private chats only: direct messages are never shown in a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_write, DirectAction.filter(F.action == "write"))
    router.callback_query.register(on_reply, DirectAction.filter(F.action == "reply"))
    router.callback_query.register(on_cancel, DirectAction.filter(F.action == "cancel"))
    router.callback_query.register(on_send, DirectAction.filter(F.action == "send"))
    router.callback_query.register(on_open, DirectAction.filter(F.action == "open"))
    # Commands still reach their own handlers while a message is being written.
    router.message.register(
        on_compose,
        F.func(lambda m: not (m.text or "").startswith("/")),
        Compose.message,
    )
    router.message.register(
        on_compose,
        F.func(lambda m: not (m.text or "").startswith("/")),
        Compose.preview,
    )
    return router
