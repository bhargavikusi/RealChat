"""Finding a chat partner and the chat keyboard (D-027 to D-033).

🎭 New Anonymous Chat starts a search in the Redis queue (D-027); the worker's
matcher pairs searchers and calls `ChatNotifier` once a match is committed. While in
a chat, the keyboard offers 👤 Partner profile and ⏹ End chat (D-030). Relaying
messages, the full "chat ended" options, and private mode come with later work.

As elsewhere: private chats only, and every database transaction is ended before
any Telegram call. A search's Cancel button carries that search's id, so a button
from an older search cannot cancel a newer one.
"""

import logging
import time
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.avatars import Avatars
from apps.bot.keyboards import END_CHAT, NEW_CHAT, PARTNER_PROFILE, chat_keyboard, main_keyboard
from apps.bot.onboarding import load_context, prompt_next
from apps.bot.profile import profile_caption
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.chat import service as chat
from backend.modules.matching.queue import SearchQueue
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from backend.modules.users.validation import Gender

logger = logging.getLogger(__name__)


class SearchAction(CallbackData, prefix="search"):
    action: str  # "start" or "cancel"
    search: str = ""  # the search a "cancel" button belongs to


SEARCHING = "🔎 Looking for someone to chat with…"
ALREADY_SEARCHING = "🔎 Still looking for someone to chat with…"
CANCEL_BUTTON = "Cancel search"
TRY_AGAIN_BUTTON = "🔁 Try again"
SEARCH_CANCELLED = "Search cancelled."
SEARCH_ENDED = "This search has already ended."
NO_ONE_FOUND = "No one is available right now. Please try again in a little while."
MATCHED = "🎭 You're connected! Say hi."
PARTNER_HEADER = "Your chat partner:"
ALREADY_IN_CHAT = f"You're already in a chat. Tap {END_CHAT} to leave it first."
NOT_IN_CHAT = f"You're not in a chat right now. Tap {NEW_CHAT} to find someone."
YOU_ENDED = "You ended the chat."
PARTNER_ENDED = "Your chat partner ended the chat."


def _button(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def _cancel_keyboard(search_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(CANCEL_BUTTON, SearchAction(action="cancel", search=search_id))]]
    )


def _try_again_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(TRY_AGAIN_BUTTON, SearchAction(action="start"))]]
    )


async def send_card(
    bot: Bot,
    avatars: Avatars,
    chat_id: int,
    profile: Profile,
    header: str,
    reply_markup: ReplyKeyboardMarkup | None = None,
) -> None:
    """Send `profile` as others see it (D-029), with the photo or the default avatar."""
    caption = f"{header}\n\n{profile_caption(profile, datetime.now(UTC).date())}"
    if profile.photo_file_id is not None:
        await bot.send_photo(
            chat_id, profile.photo_file_id, caption=caption, reply_markup=reply_markup
        )
        return
    gender = Gender(profile.gender) if profile.gender else Gender.OTHER
    await avatars.send(bot, chat_id, gender, caption, reply_markup)


async def _telegram_id(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(select(User.telegram_user_id).where(User.id == user_id))
    ).scalar_one()


# --- Searching -----------------------------------------------------------------------


async def _start_search(
    bot: Bot,
    chat_id: int,
    telegram_user: TelegramUser,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    search_queue: SearchQueue,
) -> None:
    ctx = await load_context(session, telegram_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await prompt_next(bot, chat_id, session, ctx, state, settings)
        return
    active = await chat.active_chat(session, ctx.user.id)
    await session.commit()
    if active is not None:
        await bot.send_message(chat_id, ALREADY_IN_CHAT, reply_markup=chat_keyboard())
        return
    # Any half-finished profile edit is abandoned, so its answers can't be mixed up
    # with what the user sends next.
    await state.clear()
    search_id, created = await search_queue.start(ctx.user.id, time.time())
    await bot.send_message(
        chat_id,
        SEARCHING if created else ALREADY_SEARCHING,
        reply_markup=_cancel_keyboard(search_id),
    )


async def on_new_chat(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    search_queue: SearchQueue,
) -> None:
    if message.from_user is None:
        return
    await _start_search(
        bot, message.chat.id, message.from_user, session, state, settings, search_queue
    )


async def on_try_again(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    search_queue: SearchQueue,
) -> None:
    await query.answer()
    await _start_search(
        bot, query.from_user.id, query.from_user, session, state, settings, search_queue
    )


async def on_cancel_search(
    query: CallbackQuery,
    bot: Bot,
    callback_data: SearchAction,
    session: AsyncSession,
    search_queue: SearchQueue,
) -> None:
    user = await users.get_or_create_user(session, query.from_user.id)
    await session.commit()
    if not callback_data.search or not await search_queue.cancel(user.id, callback_data.search):
        await query.answer(SEARCH_ENDED)
        return
    await query.answer()
    await bot.send_message(query.from_user.id, SEARCH_CANCELLED, reply_markup=main_keyboard())


# --- In a chat -----------------------------------------------------------------------


async def on_partner_profile(
    message: Message, bot: Bot, session: AsyncSession, avatars: Avatars
) -> None:
    if message.from_user is None:
        return
    user = await users.get_or_create_user(session, message.from_user.id)
    active = await chat.active_chat(session, user.id)
    partner = None if active is None else await users.get_profile(session, active.partner_id)
    await session.commit()
    if partner is None:
        await message.answer(NOT_IN_CHAT, reply_markup=main_keyboard())
        return
    await send_card(bot, avatars, message.chat.id, partner, PARTNER_HEADER)


async def end_chat_for(session: AsyncSession, bot: Bot, telegram_user_id: int) -> bool:
    """End the user's chat, if any, and tell their partner. Returns whether one ended.

    Commits before notifying. The partner is told on a best-effort basis: if they have
    blocked the bot, the chat still ends.
    """
    user = await users.get_or_create_user(session, telegram_user_id)
    ended = await chat.end_conversation(session, user.id, datetime.now(UTC))
    partner_telegram_id = None if ended is None else await _telegram_id(session, ended.partner_id)
    await session.commit()
    if partner_telegram_id is None:
        return False
    try:
        await bot.send_message(partner_telegram_id, PARTNER_ENDED, reply_markup=main_keyboard())
    except TelegramAPIError as exc:
        logger.warning("could not tell a partner the chat ended", extra=exception_summary(exc))
    return True


async def on_end_chat(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    if await end_chat_for(session, bot, message.from_user.id):
        await message.answer(YOU_ENDED, reply_markup=main_keyboard())
    else:
        await message.answer(NOT_IN_CHAT, reply_markup=main_keyboard())


# --- Notifications from the matcher --------------------------------------------------


class ChatNotifier:
    """Tells users about matches and ended searches. Used by the worker's matcher."""

    def __init__(
        self, bot: Bot, sessions: async_sessionmaker[AsyncSession], avatars: Avatars
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._avatars = avatars

    async def matched(self, conversation_id: int, first: int, second: int) -> None:
        async with self._sessions() as session:
            people = {
                user_id: (
                    await _telegram_id(session, user_id),
                    await users.get_profile(session, user_id),
                )
                for user_id in (first, second)
            }
            await session.commit()
        for user_id, partner_id in ((first, second), (second, first)):
            telegram_id, _ = people[user_id]
            _, partner = people[partner_id]
            try:
                await send_card(
                    self._bot, self._avatars, telegram_id, partner, MATCHED, chat_keyboard()
                )
            except TelegramAPIError as exc:
                logger.warning("could not announce a match", extra=exception_summary(exc))

    async def search_expired(self, user_id: int) -> None:
        async with self._sessions() as session:
            telegram_id = await _telegram_id(session, user_id)
            await session.commit()
        await self._bot.send_message(telegram_id, NO_ONE_FOUND, reply_markup=_try_again_keyboard())


def create_router() -> Router:
    router = Router(name="chat")
    # Private chats only: searches and chats are never run from a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.message.register(on_new_chat, F.text == NEW_CHAT)
    router.message.register(on_partner_profile, F.text == PARTNER_PROFILE)
    router.message.register(on_end_chat, F.text == END_CHAT)
    router.callback_query.register(on_try_again, SearchAction.filter(F.action == "start"))
    router.callback_query.register(on_cancel_search, SearchAction.filter(F.action == "cancel"))
    return router
