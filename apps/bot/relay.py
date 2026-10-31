"""Relaying messages between matched users (D-028, D-036, D-038, D-042, D-043).

A message from a user in an open conversation is copied to their partner with
`copyMessage`, which never shows who sent it, and with protected content, so
Telegram apps that honour it block forwarding and saving (D-036). Every message is
checked against the sender's open conversation in PostgreSQL first. Contact cards,
locations, and text with links, phone numbers, or @usernames (including links and
mentions hidden in formatting, and link buttons) are refused (D-042).

Order of work, so no transaction is held during a Telegram call:
1. Check the conversation and the content, then record the message and commit.
   Recording first means a redelivered update finds the record and stops, so a
   message is relayed at most once, even if the bot stops right after sending it.
2. Copy the message to the partner.
3. Link the record to the copy in a transaction that share-locks the conversation.
   If the chat ended while the copy was on its way, or this step fails, the copy is
   deleted again (best effort: Telegram can refuse) rather than left with a former
   partner.

Replies point at the matching message on the partner's side, and edits to text and
captions are mirrored. Media sent just after the partner turned Private mode off, or
after its time limit, is held until the sender confirms (D-049). Messages from users
who are not in a chat, commands, and anything sent while another step is waiting for an
answer go to the other handlers.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType, ContentType, MessageEntityType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    MessageId,
    ReplyParameters,
)
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.endings import PARTNER_UNREACHABLE, send_chat_ended
from apps.bot.onboarding import load_context, prompt_next
from apps.bot.private import view_once_placeholder
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import private
from backend.modules.chat import service as chat
from backend.modules.chat.content import Blocked, blocked_text
from backend.modules.chat.models import VIEW_ONCE_KINDS
from backend.modules.moderation import service as moderation
from backend.modules.users import service as users

logger = logging.getLogger(__name__)

BLOCKED = {
    Blocked.CONTACT: "Contact cards can't be shared in RealChat, so you both stay anonymous.",
    Blocked.LOCATION: "Locations can't be shared in RealChat, so you both stay anonymous.",
    Blocked.LINK: "Links can't be sent in RealChat. Your message wasn't delivered.",
    Blocked.PHONE: "Phone numbers can't be sent in RealChat. Your message wasn't delivered.",
    Blocked.USERNAME: "@usernames can't be sent in RealChat. Your message wasn't delivered.",
}
EDIT_BLOCKED = "Your edit wasn't sent to your partner: {reason}"
UNSUPPORTED = "This kind of message can't be sent in RealChat."

# Telegram descriptions meaning the partner's chat is gone, not that the message is bad.
_GONE = ("chat not found", "user is deactivated", "bot was blocked")

_KINDS = {
    ContentType.TEXT: "text",
    ContentType.PHOTO: "photo",
    ContentType.VIDEO: "video",
    ContentType.ANIMATION: "animation",
    ContentType.AUDIO: "audio",
    ContentType.VOICE: "voice",
    ContentType.VIDEO_NOTE: "video_note",
    ContentType.STICKER: "sticker",
    ContentType.DOCUMENT: "document",
    ContentType.POLL: "poll",
    ContentType.DICE: "dice",
}
_REFUSED_TYPES = {
    ContentType.CONTACT: Blocked.CONTACT,
    ContentType.LOCATION: Blocked.LOCATION,
    ContentType.VENUE: Blocked.LOCATION,
}


def _media_seconds(message: Message) -> int | None:
    """The length of a video, voice, or video message, for the view-once timer."""
    media = message.video or message.voice or message.video_note
    return None if media is None else media.duration


def _file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    media = (
        message.video
        or message.animation
        or message.audio
        or message.voice
        or message.video_note
        or message.sticker
        or message.document
    )
    return None if media is None else media.file_id


def _hidden_refusal(
    entities: list[MessageEntity] | None, markup: InlineKeyboardMarkup | None
) -> Blocked | None:
    """Links and mentions hidden in formatting, and buttons that open links.

    A word can link to a URL ("text_link") or to a Telegram user ("text_mention")
    without the link appearing in the text, and messages sent through other bots can
    carry link buttons, which a copy keeps.
    """
    for entity in entities or ():
        if entity.type == MessageEntityType.TEXT_LINK:
            return Blocked.LINK
        if entity.type == MessageEntityType.TEXT_MENTION:
            return Blocked.USERNAME
    for row in markup.inline_keyboard if markup else ():
        if any(button.url or button.login_url or button.web_app for button in row):
            return Blocked.LINK
    return None


def _text_refusal(text: str | None, entities: list[MessageEntity] | None) -> Blocked | None:
    hidden = _hidden_refusal(entities, None)
    if hidden is not None:
        return hidden
    return None if text is None else blocked_text(text)


def _refusal(message: Message) -> Blocked | None:
    refused = _REFUSED_TYPES.get(ContentType(message.content_type))
    if refused is not None:
        return refused
    hidden = _hidden_refusal(None, message.reply_markup)
    if hidden is not None:
        return hidden
    return _text_refusal(
        message.text or message.caption, message.entities or message.caption_entities
    )


def _unreachable(error: TelegramAPIError) -> bool:
    if isinstance(error, TelegramForbiddenError):
        return True
    return any(marker in error.message.casefold() for marker in _GONE)


async def _end_unreachable(
    chat_id: int, bot: Bot, session: AsyncSession, user_id: int, active: chat.ActiveChat
) -> None:
    ended = await chat.end_conversation(
        session,
        user_id,
        datetime.now(UTC),
        reason="unreachable",
        conversation_id=active.conversation_id,
    )
    if ended is None:
        # The chat already ended another way, and the user was told then.
        await session.commit()
        return
    blocked = await moderation.is_blocked(session, user_id, active.partner_id)
    await session.commit()
    await send_chat_ended(
        bot, chat_id, PARTNER_UNREACHABLE, active.conversation_id, blocked=blocked
    )


@dataclass(frozen=True)
class Outgoing:
    """What is needed to relay a message the sender sent, now or after a question."""

    message_id: int  # in the sender's chat
    kind: str
    text: str | None
    file_id: str | None
    media_seconds: int | None
    replied_message_id: int | None  # the message it replies to, in the sender's chat


def _outgoing(message: Message) -> Outgoing:
    return Outgoing(
        message_id=message.message_id,
        kind=_KINDS.get(ContentType(message.content_type), "other"),
        text=message.text or message.caption,
        file_id=_file_id(message),
        media_seconds=_media_seconds(message),
        replied_message_id=(
            None if message.reply_to_message is None else message.reply_to_message.message_id
        ),
    )


async def on_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    if message.from_user is None:
        return
    ctx = await load_context(session, message.from_user)
    active = await chat.active_chat(session, ctx.user.id)
    if active is None:
        await session.commit()
        raise SkipHandler  # not in a chat: other handlers answer it
    if not ctx.terms_accepted:
        # Messages are stored, so the current terms (which say so) come first.
        await prompt_next(bot, message.chat.id, session, ctx, state, settings)
        return
    refusal = _refusal(message)
    if refusal is not None:
        await session.commit()
        await message.answer(BLOCKED[refusal])
        return
    outgoing = _outgoing(message)
    if outgoing.kind in VIEW_ONCE_KINDS:
        window = await runtime_settings.get(session, SettingKey.PRIVATE_ENDED_ASK_SECONDS)
        reason = (
            await private.ended_by_someone_else(
                session,
                active.conversation_id,
                ctx.user.id,
                datetime.now(UTC) - timedelta(seconds=window),
            )
            if window > 0
            else None
        )
        if reason is not None:
            await session.commit()
            await hold(message.chat.id, bot, state, active, outgoing, reason)
            return
    await relay(bot, session, runtime_settings, message.chat.id, ctx.user.id, active, outgoing)


async def relay(
    bot: Bot,
    session: AsyncSession,
    runtime_settings: RuntimeSettings,
    sender_chat_id: int,
    sender_id: int,
    active: chat.ActiveChat,
    outgoing: Outgoing,
) -> None:
    """Record a message, copy it to the partner, and link the copy (see the module notes)."""
    target = None
    if outgoing.replied_message_id is not None:
        target = await chat.reply_target(session, active, sender_id, outgoing.replied_message_id)
    # If this is view-once media, how long it can be seen once opened, fixed now (D-048).
    view_seconds = (
        await runtime_settings.get(session, SettingKey.VIEW_ONCE_SECONDS)
        + (outgoing.media_seconds or 0)
        if outgoing.kind in VIEW_ONCE_KINDS
        else None
    )
    # Recorded before the copy is sent: a redelivered update finds this row and stops,
    # so a message is relayed at most once even if the bot stops right after sending.
    claim = await chat.claim_message(
        session,
        active,
        sender_id,
        outgoing.message_id,
        outgoing.kind,
        outgoing.text,
        outgoing.file_id,
        view_seconds,
    )
    partner = await users.get_user(session, active.partner_id)
    await session.commit()
    if claim is None:
        return  # already relayed, or the chat ended meanwhile

    reply = (
        None
        if target is None
        else ReplyParameters(message_id=target, allow_sending_without_reply=True)
    )
    try:
        if claim.view_once:
            # Private mode media is sent as a tap-to-view placeholder (D-048).
            text, button = view_once_placeholder(
                outgoing.kind, outgoing.media_seconds, view_seconds or 0, claim.row_id
            )
            copied: MessageId | Message = await bot.send_message(
                partner.telegram_user_id,
                text,
                reply_markup=button,
                protect_content=True,
                reply_parameters=reply,
            )
        else:
            copied = await bot.copy_message(
                chat_id=partner.telegram_user_id,
                from_chat_id=sender_chat_id,
                message_id=outgoing.message_id,
                protect_content=True,
                reply_parameters=reply,
            )
    except TelegramAPIError as error:
        await chat.discard_message(session, claim)
        if _unreachable(error):
            await _end_unreachable(sender_chat_id, bot, session, sender_id, active)
            return
        await session.commit()
        if isinstance(error, TelegramBadRequest):
            await bot.send_message(sender_chat_id, UNSUPPORTED)
            return
        raise

    try:
        delivered = await chat.complete_message(session, claim, copied.message_id)
        await session.commit()
    except BaseException:
        # The copy exists but could not be linked: take it back rather than leave a
        # message the database does not know about, possibly with a former partner.
        await _withdraw(bot, partner.telegram_user_id, copied.message_id)
        raise
    if not delivered:
        # The chat ended while the copy was on its way: take it back.
        await _withdraw(bot, partner.telegram_user_id, copied.message_id)


# --- Media sent just after Private mode ended (D-049) -----------------------------------


class HeldAction(CallbackData, prefix="held"):
    action: str  # "send" or "drop"
    message: int  # the held message, by its id in the sender's chat


HELD_STATE_KEY = "held"
_KIND_NAMES = {
    "photo": "photo",
    "video": "video",
    "voice": "voice message",
    "video_note": "video message",
}
HELD_BY_PARTNER = (
    "Your chat partner turned off Private mode, so this {kind} would be sent as a normal "
    "message. Send it?"
)
HELD_BY_TIME_LIMIT = (
    "Private mode ended at its time limit, so this {kind} would be sent as a normal "
    "message. Send it?"
)
SEND_BUTTON = "Send"
DROP_BUTTON = "Don't send"
NOT_SENT = "Not sent."
HELD_EXPIRED = "This has expired. Send it again if you still want to."


async def hold(
    chat_id: int,
    bot: Bot,
    state: FSMContext,
    active: chat.ActiveChat,
    outgoing: Outgoing,
    reason: str,
) -> None:
    """Keep media back and ask whether to send it normally. Only its details are kept,
    in the user's short-lived bot state; the media itself stays in their own chat."""
    held = dict((await state.get_data()).get(HELD_STATE_KEY, {}))
    held[str(outgoing.message_id)] = {
        "conversation": active.conversation_id,
        **asdict(outgoing),
    }
    await state.update_data({HELD_STATE_KEY: held})
    template = HELD_BY_TIME_LIMIT if reason == "time_limit" else HELD_BY_PARTNER
    buttons = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=HeldAction(action=action, message=outgoing.message_id).pack(),
                )
                for label, action in ((SEND_BUTTON, "send"), (DROP_BUTTON, "drop"))
            ]
        ]
    )
    await bot.send_message(
        chat_id,
        template.format(kind=_KIND_NAMES[outgoing.kind]),
        reply_markup=buttons,
        reply_parameters=ReplyParameters(
            message_id=outgoing.message_id, allow_sending_without_reply=True
        ),
    )


async def on_held(
    query: CallbackQuery,
    bot: Bot,
    callback_data: HeldAction,
    session: AsyncSession,
    state: FSMContext,
    runtime_settings: RuntimeSettings,
) -> None:
    """[Send] or [Don't send] for held media. Each question is answered once."""
    held = dict((await state.get_data()).get(HELD_STATE_KEY, {}))
    details = held.pop(str(callback_data.message), None)
    await state.update_data({HELD_STATE_KEY: held})
    await _remove_buttons(bot, query)
    if details is None:
        await query.answer(HELD_EXPIRED)
        return
    if callback_data.action != "send":
        await query.answer(NOT_SENT)
        return
    user = await users.get_or_create_user(session, query.from_user.id)
    active = await chat.active_chat(session, user.id)
    if (
        active is None
        or active.conversation_id != details.pop("conversation")
        or await private.active_session(session, active.conversation_id) is not None
    ):
        # The chat ended, or Private mode is on again: the question no longer applies.
        await session.commit()
        await query.answer(HELD_EXPIRED)
        return
    await query.answer()
    await relay(
        bot,
        session,
        runtime_settings,
        query.from_user.id,
        user.id,
        active,
        Outgoing(**details),
    )


async def _remove_buttons(bot: Bot, query: CallbackQuery) -> None:
    if query.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=None
        )
    except TelegramAPIError as exc:
        logger.info("could not remove buttons", extra=exception_summary(exc))


async def _withdraw(bot: Bot, chat_id: int, message_id: int) -> None:
    """Delete a copy from the partner's chat. Best effort: Telegram may refuse."""
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as exc:
        logger.warning("could not withdraw a copy", extra=exception_summary(exc))


async def on_edit(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    """Mirror an edited text or caption onto the partner's copy (D-038)."""
    if message.from_user is None:
        return
    new_text = message.text if message.text is not None else message.caption
    if new_text is None:
        return  # only text and captions are mirrored
    ctx = await load_context(session, message.from_user)
    active = await chat.active_chat(session, ctx.user.id)
    relayed = await chat.relayed_message(session, ctx.user.id, message.message_id)
    if (
        active is None
        or relayed is None
        or relayed.recipient_message_id is None
        or relayed.conversation_id != active.conversation_id
    ):
        await session.commit()
        return  # not a delivered message of the current chat
    if not ctx.terms_accepted:
        # As for new messages: nothing is stored or sent until the current terms are.
        await prompt_next(bot, message.chat.id, session, ctx, state, settings)
        return
    refusal = _text_refusal(new_text, message.entities or message.caption_entities)
    if refusal is not None:
        await session.commit()
        await message.answer(EDIT_BLOCKED.format(reason=BLOCKED[refusal]))
        return
    chat.record_edit(relayed, new_text, datetime.now(UTC))
    # View-once media: the partner has a placeholder, and the edit shows when it is
    # opened; once opened, it goes to the opened copy (D-048).
    recipient_message_id = (
        relayed.viewed_message_id if relayed.view_once else relayed.recipient_message_id
    )
    if recipient_message_id is None:
        await session.commit()
        return
    partner = await users.get_user(session, active.partner_id)
    await session.commit()
    try:
        if message.text is not None:
            await bot.edit_message_text(
                text=new_text,
                chat_id=partner.telegram_user_id,
                message_id=recipient_message_id,
                entities=message.entities,
            )
        else:
            await bot.edit_message_caption(
                chat_id=partner.telegram_user_id,
                message_id=recipient_message_id,
                caption=new_text,
                caption_entities=message.caption_entities,
            )
    except TelegramAPIError as exc:
        # e.g. the partner deleted their copy, or nothing visible changed.
        logger.info("edit not mirrored", extra=exception_summary(exc))


def create_router() -> Router:
    router = Router(name="relay")
    # Private chats only, and only when no other step is waiting for an answer.
    router.message.filter(F.chat.type == ChatType.PRIVATE, StateFilter(None))
    router.edited_message.filter(F.chat.type == ChatType.PRIVATE, StateFilter(None))
    # Commands are never relayed; they reach their own handlers.
    router.message.register(on_message, ~F.text.startswith("/"))
    router.edited_message.register(on_edit)
    # The answer to a held-media question (D-049); it needs no state filter.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_held, HeldAction.filter())
    return router
