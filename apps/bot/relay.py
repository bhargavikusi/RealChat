"""Relaying messages between matched users (D-028, D-036, D-038, D-042, D-043).

A message from a user in an open conversation is copied to their partner with
`copyMessage`, which never shows who sent it, and with protected content, so it
can't be forwarded or saved. Every message is checked against the sender's open
conversation in PostgreSQL first. Contact cards, locations, and text with links,
phone numbers, or @usernames are refused (D-042).

Order of work, so no transaction is held during a Telegram call:
1. Check the conversation, the content, and duplicates, then commit.
2. Copy the message to the partner.
3. Store it in a new transaction that share-locks the conversation. If the chat
   ended while the copy was on its way, the copy is deleted again rather than left
   with a former partner.

Replies point at the matching message on the partner's side, and edits to text and
captions are mirrored. Messages from users who are not in a chat, commands, and
anything sent while another step is waiting for an answer go to the other handlers.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards import main_keyboard
from apps.bot.onboarding import load_context, prompt_next
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.chat import service as chat
from backend.modules.chat.content import Blocked, blocked_text
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
PARTNER_UNREACHABLE = (
    "Your chat partner can't be reached any more, so the chat has ended. "
    "Your last message wasn't delivered."
)

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


def _refusal(message: Message) -> Blocked | None:
    refused = _REFUSED_TYPES.get(ContentType(message.content_type))
    if refused is not None:
        return refused
    text = message.text or message.caption
    return None if text is None else blocked_text(text)


def _unreachable(error: TelegramAPIError) -> bool:
    if isinstance(error, TelegramForbiddenError):
        return True
    return any(marker in error.message.casefold() for marker in _GONE)


async def _end_unreachable(message: Message, session: AsyncSession, user_id: int) -> None:
    await chat.end_conversation(session, user_id, datetime.now(UTC), reason="unreachable")
    await session.commit()
    await message.answer(PARTNER_UNREACHABLE, reply_markup=main_keyboard())


async def on_message(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
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
    if await chat.relayed_message(session, ctx.user.id, message.message_id) is not None:
        await session.commit()
        return  # already relayed (a redelivered update)
    target = None
    if message.reply_to_message is not None:
        target = await chat.reply_target(
            session, active, ctx.user.id, message.reply_to_message.message_id
        )
    partner = await users.get_user(session, active.partner_id)
    await session.commit()

    try:
        copied = await bot.copy_message(
            chat_id=partner.telegram_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            protect_content=True,
            reply_parameters=(
                None
                if target is None
                else ReplyParameters(message_id=target, allow_sending_without_reply=True)
            ),
        )
    except TelegramAPIError as error:
        if _unreachable(error):
            await _end_unreachable(message, session, ctx.user.id)
            return
        if isinstance(error, TelegramBadRequest):
            await message.answer(UNSUPPORTED)
            return
        raise

    kind = _KINDS.get(ContentType(message.content_type), "other")
    stored = await chat.record_message(
        session,
        active,
        ctx.user.id,
        message.message_id,
        copied.message_id,
        kind,
        message.text or message.caption,
        _file_id(message),
    )
    await session.commit()
    if not stored:
        # The chat ended while the copy was on its way: take it back.
        try:
            await bot.delete_message(partner.telegram_user_id, copied.message_id)
        except TelegramAPIError as exc:
            logger.warning("could not withdraw a late copy", extra=exception_summary(exc))


async def on_edit(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Mirror an edited text or caption onto the partner's copy (D-038)."""
    if message.from_user is None:
        return
    new_text = message.text if message.text is not None else message.caption
    if new_text is None:
        return  # only text and captions are mirrored
    user = await users.get_or_create_user(session, message.from_user.id)
    active = await chat.active_chat(session, user.id)
    relayed = await chat.relayed_message(session, user.id, message.message_id)
    if active is None or relayed is None or relayed.conversation_id != active.conversation_id:
        await session.commit()
        return  # not part of the current chat
    refusal = blocked_text(new_text)
    if refusal is not None:
        await session.commit()
        await message.answer(EDIT_BLOCKED.format(reason=BLOCKED[refusal]))
        return
    chat.record_edit(relayed, new_text, datetime.now(UTC))
    partner = await users.get_user(session, active.partner_id)
    recipient_message_id = relayed.recipient_message_id
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
    router.edited_message.filter(F.chat.type == ChatType.PRIVATE)
    # Commands are never relayed; they reach their own handlers.
    router.message.register(on_message, ~F.text.startswith("/"))
    router.edited_message.register(on_edit)
    return router
