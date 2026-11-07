"""Finding a chat partner and the chat keyboard (D-027 to D-033).

🎭 New Anonymous Chat starts a search in the Redis queue (D-027); the worker's
matcher pairs searchers and calls `ChatNotifier` once a match is committed. While in
a chat, the keyboard offers 👤 Partner profile and ⏹ End chat (D-030); the partner's
card carries Block and Report (D-045, handled in `apps.bot.safety`). Private mode
comes with later work.

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
)
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.avatars import Avatars
from apps.bot.cards import send_card
from apps.bot.endings import (
    IDLE_ENDED,
    PARTNER_ENDED,
    YOU_ENDED,
    PaidSearchAction,
    SearchAction,
    card_keyboard,
    coins_label,
    like_row,
    menu_price,
    safety_keyboard,
    send_chat_ended,
    send_menu,
    tell_chat_ended,
)
from apps.bot.keyboards import (
    END_CHAT,
    NEW_CHAT,
    PARTNER_PROFILE,
    chat_keyboard,
    current_keyboard,
    main_keyboard,
)
from apps.bot.onboarding import load_context, prompt_next
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.chat import service as chat
from backend.modules.coins import service as coins
from backend.modules.matching.queue import (
    SearchQueue,
    new_token,
    parse_search,
    search_hold_key,
    search_id_for,
)
from backend.modules.moderation import service as moderation
from backend.modules.people import likes, visits
from backend.modules.users import service as users
from backend.modules.users.models import User

logger = logging.getLogger(__name__)


SEARCHING = "🔎 Looking for someone to chat with…"
ALREADY_SEARCHING = "🔎 Still looking for someone to chat with…"
CANCEL_BUTTON = "Cancel search"
SEARCH_CANCELLED = "Search cancelled."
SEARCHING_FOR = (
    "🔎 Looking for {who} to chat with… {price} will be held and only taken if you're matched."
)
ALREADY_SEARCHING_FREE = (
    "You're already searching for a random chat. Cancel it first to choose who to meet."
)
WHO = {"male": "a man", "female": "a woman", "other": "someone of another gender"}
NOT_ENOUGH_COINS = (
    "This chat costs {price}, and you have {available} available. See /coins for ways to "
    "earn coins."
)
COINS_RETURNED = "Your held coins were returned."
COINS_USED = "🪙 {coins} used for this chat."
SEARCH_ENDED = "This search has already ended."
NO_ONE_FOUND = "No one is available right now. Please try again in a little while."
MATCHED = "🎭 You're connected! Say hi."
PARTNER_HEADER = "Your chat partner:"
ALREADY_IN_CHAT = f"You're already in a chat. Tap {END_CHAT} to leave it first."
NOT_IN_CHAT = f"You're not in a chat right now. Tap {NEW_CHAT} to find someone."
IDLE_WARNING = (
    "💤 This chat has been quiet for {quiet}. It will end in {grace} unless one of you "
    "sends a message."
)


def _button(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def _cancel_keyboard(search_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(CANCEL_BUTTON, SearchAction(action="cancel", search=search_id))]]
    )


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
    wants: str | None = None,
) -> None:
    ctx = await load_context(session, telegram_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await prompt_next(bot, chat_id, session, ctx, state, settings)
        return
    active = await chat.active_chat(session, ctx.user.id)
    keyboard = await current_keyboard(session, ctx.user.id)
    await session.commit()
    if active is not None:
        await bot.send_message(chat_id, ALREADY_IN_CHAT, reply_markup=keyboard)
        return
    # Any half-finished profile edit is abandoned, so its answers can't be mixed up
    # with what the user sends next.
    await state.clear()
    if wants is None:
        search_id, created = await search_queue.start(ctx.user.id, time.time())
        await bot.send_message(
            chat_id,
            SEARCHING if created else ALREADY_SEARCHING,
            reply_markup=_cancel_keyboard(search_id),
        )
        return
    # A paid search (D-050). Someone already searching is told so before any coins are
    # held; the queue still settles a race below.
    current = await search_queue.current_search(ctx.user.id)
    if current is not None:
        await _tell_already_searching(bot, chat_id, current)
        return
    # The price is held first, and taken only on a match.
    price = await menu_price(session)
    token = new_token()
    try:
        await coins.hold(
            session, ctx.user.id, price, "paid_match", search_hold_key(ctx.user.id, token)
        )
        await session.commit()
    except coins.InsufficientCoinsError as error:
        await session.rollback()
        await bot.send_message(
            chat_id,
            NOT_ENOUGH_COINS.format(
                price=coins_label(price), available=coins_label(error.available)
            ),
        )
        return
    try:
        search_id, created = await search_queue.start(
            ctx.user.id, time.time(), search_id_for(token, wants)
        )
    except BaseException:
        await _release_search_hold(session, ctx.user.id, token)
        raise
    if not created:
        # Already searching: that search goes on, and this hold is given back.
        await _release_search_hold(session, ctx.user.id, token)
        await _tell_already_searching(bot, chat_id, search_id)
        return
    await bot.send_message(
        chat_id,
        SEARCHING_FOR.format(who=WHO[wants], price=coins_label(price)),
        reply_markup=_cancel_keyboard(search_id),
    )


async def _tell_already_searching(bot: Bot, chat_id: int, search_id: str) -> None:
    """A paid tap while a search runs: say which kind is running, with its Cancel button."""
    free = parse_search(search_id).wants is None
    await bot.send_message(
        chat_id,
        ALREADY_SEARCHING_FREE if free else ALREADY_SEARCHING,
        reply_markup=_cancel_keyboard(search_id),
    )


async def _release_search_hold(session: AsyncSession, user_id: int, token: str) -> bool:
    """Give back the coins held for a paid search; returns whether any were."""
    hold_id = await coins.hold_id_for(session, search_hold_key(user_id, token))
    released = hold_id is not None and await coins.release(session, hold_id, datetime.now(UTC))
    await session.commit()
    return released


async def on_new_chat(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    """🎭 opens the menu of chats to choose from (D-044)."""
    if message.from_user is None:
        return
    ctx = await load_context(session, message.from_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await prompt_next(bot, message.chat.id, session, ctx, state, settings)
        return
    active = await chat.active_chat(session, ctx.user.id)
    keyboard = await current_keyboard(session, ctx.user.id)
    price = await menu_price(session)
    await session.commit()
    # Any half-finished profile or admin edit is abandoned, so the next message is not
    # taken as its answer.
    await state.clear()
    if active is not None:
        await message.answer(ALREADY_IN_CHAT, reply_markup=keyboard)
        return
    await send_menu(bot, message.chat.id, price)


async def on_random_chat(
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


async def on_paid_chat(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PaidSearchAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    search_queue: SearchQueue,
) -> None:
    await query.answer()
    if callback_data.wants not in WHO:
        return  # not a button the bot made
    await _start_search(
        bot,
        query.from_user.id,
        query.from_user,
        session,
        state,
        settings,
        search_queue,
        callback_data.wants,
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
    search = parse_search(callback_data.search)
    refunded = search.wants is not None and await _release_search_hold(
        session, user.id, search.token
    )
    text = f"{SEARCH_CANCELLED} {COINS_RETURNED}" if refunded else SEARCH_CANCELLED
    await bot.send_message(query.from_user.id, text, reply_markup=main_keyboard())


# --- In a chat -----------------------------------------------------------------------


async def on_partner_profile(
    message: Message, bot: Bot, session: AsyncSession, avatars: Avatars
) -> None:
    if message.from_user is None:
        return
    user = await users.get_or_create_user(session, message.from_user.id)
    active = await chat.active_chat(session, user.id)
    if active is None:
        await session.commit()
        await message.answer(NOT_IN_CHAT, reply_markup=main_keyboard())
        return
    partner = await users.get_profile(session, active.partner_id)
    blocked = await moderation.is_blocked(session, user.id, active.partner_id)
    liked = await likes.is_liked(session, user.id, active.partner_id)
    # Listed in the partner's Visited you, without a notice during the chat (D-053).
    await visits.record_visit(session, user.id, active.partner_id, datetime.now(UTC))
    await session.commit()
    await send_card(
        bot,
        avatars,
        message.chat.id,
        partner,
        PARTNER_HEADER,
        card_keyboard(
            like_row(partner.public_id, liked), safety_keyboard(active.conversation_id, blocked)
        ),
    )


async def tell_partner_ended(
    session: AsyncSession, bot: Bot, user_id: int, ended: chat.ActiveChat
) -> None:
    """Tell the partner in a chat `user_id` just ended, with Block or Unblock as fits them.

    Commits before notifying. The partner is told on a best-effort basis: if they have
    blocked the bot, the chat still ends.
    """
    partner_telegram_id = await _telegram_id(session, ended.partner_id)
    blocked = await moderation.is_blocked(session, ended.partner_id, user_id)
    price = await menu_price(session)
    await session.commit()
    await tell_chat_ended(
        bot, partner_telegram_id, PARTNER_ENDED, ended.conversation_id, blocked=blocked, price=price
    )


async def on_end_chat(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    user = await users.get_or_create_user(session, message.from_user.id)
    ended = await chat.end_conversation(session, user.id, datetime.now(UTC))
    if ended is None:
        await session.commit()
        await message.answer(NOT_IN_CHAT, reply_markup=main_keyboard())
        return
    blocked = await moderation.is_blocked(session, user.id, ended.partner_id)
    price = await menu_price(session)
    await tell_partner_ended(session, bot, user.id, ended)
    await send_chat_ended(
        bot, message.chat.id, YOU_ENDED, ended.conversation_id, blocked=blocked, price=price
    )


# --- Notifications from the matcher --------------------------------------------------


def duration(seconds: int) -> str:
    """A setting value in words: under two minutes in seconds, otherwise in minutes."""
    if seconds < 120:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, rest = divmod(seconds, 60)
    text = f"{minutes} minutes"
    return text if not rest else f"{text} {rest} second{'s' if rest != 1 else ''}"


class ChatNotifier:
    """Tells users about matches, ended searches, and quiet chats. Used by the worker."""

    def __init__(
        self, bot: Bot, sessions: async_sessionmaker[AsyncSession], avatars: Avatars
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._avatars = avatars

    async def matched(
        self, conversation_id: int, first: int, second: int, charged: dict[int, int]
    ) -> None:
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
            # Only the payer is told what they paid; the partner never learns of it.
            header = (
                f"{MATCHED}\n{COINS_USED.format(coins=coins_label(charged[user_id]))}"
                if charged.get(user_id)
                else MATCHED
            )
            try:
                await send_card(
                    self._bot, self._avatars, telegram_id, partner, header, chat_keyboard()
                )
            except TelegramAPIError as exc:
                logger.warning("could not announce a match", extra=exception_summary(exc))

    async def idle_warning(self, user_ids: tuple[int, int], quiet: int, grace: int) -> None:
        text = IDLE_WARNING.format(quiet=duration(quiet), grace=duration(grace))
        for telegram_id in await self._telegram_ids(user_ids):
            try:
                await self._bot.send_message(telegram_id, text)
            except TelegramAPIError as exc:
                logger.warning("could not send an idle warning", extra=exception_summary(exc))

    async def idle_ended(self, conversation_id: int, user_ids: tuple[int, int]) -> None:
        first, second = user_ids
        async with self._sessions() as session:
            people = [
                (
                    await _telegram_id(session, user_id),
                    await moderation.is_blocked(session, user_id, partner_id),
                )
                for user_id, partner_id in ((first, second), (second, first))
            ]
            price = await menu_price(session)
            await session.commit()
        for telegram_id, blocked in people:
            await tell_chat_ended(
                self._bot, telegram_id, IDLE_ENDED, conversation_id, blocked=blocked, price=price
            )

    async def _telegram_ids(self, user_ids: tuple[int, int]) -> list[int]:
        async with self._sessions() as session:
            telegram_ids = [await _telegram_id(session, user_id) for user_id in user_ids]
            await session.commit()
        return telegram_ids

    async def search_expired(self, user_id: int, refunded: bool) -> None:
        async with self._sessions() as session:
            telegram_id = await _telegram_id(session, user_id)
            price = await menu_price(session)
            await session.commit()
        text = f"{NO_ONE_FOUND} {COINS_RETURNED}" if refunded else NO_ONE_FOUND
        await send_menu(self._bot, telegram_id, price, text)


def create_router() -> Router:
    router = Router(name="chat")
    # Private chats only: searches and chats are never run from a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.message.register(on_new_chat, F.text == NEW_CHAT)
    router.message.register(on_partner_profile, F.text == PARTNER_PROFILE)
    router.message.register(on_end_chat, F.text == END_CHAT)
    router.callback_query.register(on_random_chat, SearchAction.filter(F.action == "start"))
    router.callback_query.register(on_paid_chat, PaidSearchAction.filter())
    router.callback_query.register(on_cancel_search, SearchAction.filter(F.action == "cancel"))
    return router
