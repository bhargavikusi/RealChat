"""People: past chat partners the user can reach again (D-051).

👥 People sits on the user's own profile card. It opens 💬 Past chats: everyone the
user chatted with whose chat ended within the window (an admin setting), latest
first, in pages. Tapping someone shows their card with Block (or Unblock) and Report
for that chat (D-045). Blocks hide nobody; the blocker sees 🚫 beside the name.

Every button carries what it lists or opens, and the server checks it again: a
partner button opens a card only while that chat still counts as a past chat, so an
old or forged button opens nobody. Like the profile, People needs the current terms
and a finished onboarding. The wording and layout are provisional until the design
pass. As elsewhere, every database transaction is ended before any Telegram call.
"""

from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.avatars import Avatars
from apps.bot.cards import send_card
from apps.bot.endings import safety_keyboard
from apps.bot.onboarding import load_context, prompt_next
from backend.config.settings import BotSettings
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.moderation import service as moderation
from backend.modules.people import service as people


class PeopleAction(CallbackData, prefix="people"):
    action: str  # "menu", "past" (a page of past chats), or "open" (a partner's card)
    page: int = 0
    conversation: int = 0  # the past chat an "open" button belongs to


PEOPLE_BUTTON = "👥 People"
PEOPLE_PROMPT = "👥 People"
PAST_CHATS_BUTTON = "💬 Past chats"
PAST_CHATS_HEADER = "💬 Past chats from the last {days}"
NO_PAST_CHATS = "No past chats from the last {days}."
PREVIOUS_BUTTON = "◀️ Previous"
NEXT_BUTTON = "Next ▶️"
PAST_CHAT_HEADER = "💬 Past chat"
NOT_AVAILABLE = "This person isn't available any more."
BLOCKED_MARK = " 🚫"


def _now() -> datetime:
    return datetime.now(UTC)


def _button(text: str, data: PeopleAction) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def people_button() -> InlineKeyboardButton:
    return _button(PEOPLE_BUTTON, PeopleAction(action="menu"))


def days_label(days: int) -> str:
    return f"{days} day{'s' if days != 1 else ''}"


def ago(ended_at: datetime, now: datetime) -> str:
    days = (now.date() - ended_at.astimezone(UTC).date()).days
    if days <= 0:
        return "today"
    return "yesterday" if days == 1 else f"{days} days ago"


def _entry(entry: people.PastChat, now: datetime) -> InlineKeyboardButton:
    mark = BLOCKED_MARK if entry.blocked else ""
    return _button(
        f"{entry.nickname} · {ago(entry.ended_at, now)}{mark}",
        PeopleAction(action="open", conversation=entry.conversation_id),
    )


def past_chats_keyboard(page: int, found: people.PastChats, now: datetime) -> InlineKeyboardMarkup:
    rows = [[_entry(entry, now)] for entry in found.entries]
    nav = []
    if page > 0:
        nav.append(_button(PREVIOUS_BUTTON, PeopleAction(action="past", page=page - 1)))
    if found.has_next:
        nav.append(_button(NEXT_BUTTON, PeopleAction(action="past", page=page + 1)))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ready(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> int | None:
    """The user's id once they have accepted the current terms and finished onboarding.

    Otherwise they are shown the terms or their onboarding step, and None is returned.
    """
    ctx = await load_context(session, query.from_user)
    if ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None:
        return ctx.user.id
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, ctx, state, settings)
    return None


async def on_menu(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if await _ready(query, bot, session, state, settings) is None:
        return
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        PEOPLE_PROMPT,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_button(PAST_CHATS_BUTTON, PeopleAction(action="past"))]]
        ),
    )


async def on_past_chats(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await _ready(query, bot, session, state, settings)
    if user_id is None:
        return
    days = await runtime_settings.get(session, SettingKey.PAST_CHATS_DAYS)
    now = _now()
    page = max(callback_data.page, 0)
    found = await people.past_chats(session, user_id, people.window_start(now, days), page)
    await session.commit()
    await query.answer()
    if not found.entries and page == 0:
        await bot.send_message(query.from_user.id, NO_PAST_CHATS.format(days=days_label(days)))
        return
    await bot.send_message(
        query.from_user.id,
        PAST_CHATS_HEADER.format(days=days_label(days)),
        reply_markup=past_chats_keyboard(page, found, now),
    )


async def on_open(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    avatars: Avatars,
) -> None:
    user_id = await _ready(query, bot, session, state, settings)
    if user_id is None:
        return
    days = await runtime_settings.get(session, SettingKey.PAST_CHATS_DAYS)
    since = people.window_start(_now(), days)
    conversation_id = callback_data.conversation
    profile = await people.past_partner(session, user_id, conversation_id, since)
    if profile is None:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    blocked = await moderation.is_blocked(session, user_id, profile.user_id)
    await session.commit()
    await query.answer()
    await send_card(
        bot,
        avatars,
        query.from_user.id,
        profile,
        PAST_CHAT_HEADER,
        safety_keyboard(conversation_id, blocked),
    )


def create_router() -> Router:
    router = Router(name="people")
    # Private chats only: other users' profiles must never be posted in a group.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_menu, PeopleAction.filter(F.action == "menu"))
    router.callback_query.register(on_past_chats, PeopleAction.filter(F.action == "past"))
    router.callback_query.register(on_open, PeopleAction.filter(F.action == "open"))
    return router
