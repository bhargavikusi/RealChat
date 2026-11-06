"""People: past chat partners the user can reach again (D-051), and visits (D-053).

👥 People sits on the user's own profile card. It opens 💬 Past chats: everyone the
user chatted with whose chat ended within the window (an admin setting), latest
first, in pages. Tapping someone shows their card with Block (or Unblock) and Report
for that chat (D-045). Blocks hide nobody; the blocker sees 🚫 beside the name.

Every button carries what it lists or opens, and the server checks it again: a
partner button opens a card only while that chat still counts as a past chat, so an
old or forged button opens nobody.

👀 Visited you lists everyone who opened the user's card, once each, latest visit
first. Opening someone's card from People records a visit and tells them, unless they
turned visit alerts off, blocked the visitor, or the two are chatting together. A
visitor's card opens from the list or from that notice while the visit is kept. Like
the profile, People needs the current terms
and a finished onboarding. The wording and layout are provisional until the design
pass. As elsewhere, every database transaction is ended before any Telegram call.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.avatars import Avatars
from apps.bot.cards import send_card
from apps.bot.endings import safety_keyboard
from apps.bot.onboarding import load_context, prompt_next
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.moderation import service as moderation
from backend.modules.people import service as people
from backend.modules.people import visits
from backend.modules.users import service as users
from backend.modules.users.models import Profile

logger = logging.getLogger(__name__)


class PeopleAction(CallbackData, prefix="people"):
    # "menu", "past" (a page of past chats), "open" (a past partner's card), "visitors"
    # (a page of Visited you), or "visitor" (a visitor's card)
    action: str
    page: int = 0
    conversation: int = 0  # the past chat an "open" button belongs to
    user: int = 0  # the visitor a "visitor" button opens


PEOPLE_BUTTON = "👥 People"
PEOPLE_PROMPT = "👥 People"
PAST_CHATS_BUTTON = "💬 Past chats"
PAST_CHATS_HEADER = "💬 Past chats from the last {days}"
NO_PAST_CHATS = "No past chats from the last {days}."
PREVIOUS_BUTTON = "◀️ Previous"
NEXT_BUTTON = "Next ▶️"
PAST_CHAT_HEADER = "💬 Past chat"
NOT_AVAILABLE = "This person isn't available any more."
VISITORS_BUTTON = "👀 Visited you"
VISITORS_HEADER = "👀 Visited you in the last {days}"
NO_VISITORS = "Nobody visited your profile in the last {days}."
VISITOR_HEADER = "👀 Visited your profile"
VISIT_NOTICE = "👀 {nickname} viewed your profile."
VIEW_PROFILE_BUTTON = "👤 View profile"
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


def _visitor_entry(entry: visits.Visitor, now: datetime) -> InlineKeyboardButton:
    mark = BLOCKED_MARK if entry.blocked else ""
    return _button(
        f"{entry.nickname} · {ago(entry.visited_at, now)}{mark}",
        PeopleAction(action="visitor", user=entry.user_id),
    )


def _pages(
    rows: list[list[InlineKeyboardButton]], action: str, page: int, has_next: bool
) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(_button(PREVIOUS_BUTTON, PeopleAction(action=action, page=page - 1)))
    if has_next:
        nav.append(_button(NEXT_BUTTON, PeopleAction(action=action, page=page + 1)))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def past_chats_keyboard(page: int, found: people.PastChats, now: datetime) -> InlineKeyboardMarkup:
    rows = [[_entry(entry, now)] for entry in found.entries]
    return _pages(rows, "past", page, found.has_next)


def visitors_keyboard(page: int, found: visits.Visitors, now: datetime) -> InlineKeyboardMarkup:
    rows = [[_visitor_entry(entry, now)] for entry in found.entries]
    return _pages(rows, "visitors", page, found.has_next)


class Notice:
    """A visit notice to send once the transaction is committed."""

    def __init__(self, chat_id: int, visitor_id: int, nickname: str) -> None:
        self.chat_id, self.visitor_id, self.nickname = chat_id, visitor_id, nickname

    async def send(self, bot: Bot) -> None:
        """Best effort: the visit is recorded even if the user can't be reached."""
        button = _button(VIEW_PROFILE_BUTTON, PeopleAction(action="visitor", user=self.visitor_id))
        try:
            await bot.send_message(
                self.chat_id,
                VISIT_NOTICE.format(nickname=self.nickname),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
            )
        except TelegramAPIError as exc:
            logger.info("could not send a visit notice", extra=exception_summary(exc))


async def _visit(
    session: AsyncSession, visitor_id: int, visited: Profile, now: datetime
) -> Notice | None:
    """Record the visit; returns the notice to send after committing, if any."""
    await visits.record_visit(session, visitor_id, visited.user_id, now)
    if not await visits.should_notify(session, visitor_id, visited.user_id):
        return None
    nickname = (await users.get_profile(session, visitor_id)).nickname
    chat_id = (await users.get_user(session, visited.user_id)).telegram_user_id
    return Notice(chat_id, visitor_id, str(nickname))


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
            inline_keyboard=[
                [_button(PAST_CHATS_BUTTON, PeopleAction(action="past"))],
                [_button(VISITORS_BUTTON, PeopleAction(action="visitors"))],
            ]
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
    notice = await _visit(session, user_id, profile, _now())
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
    if notice is not None:
        await notice.send(bot)


async def on_visitors(
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
    days = await runtime_settings.get(session, SettingKey.VISITS_DAYS)
    now = _now()
    page = max(callback_data.page, 0)
    found = await visits.visitors(session, user_id, visits.retention_start(now, days), page)
    await session.commit()
    await query.answer()
    if not found.entries and page == 0:
        await bot.send_message(query.from_user.id, NO_VISITORS.format(days=days_label(days)))
        return
    await bot.send_message(
        query.from_user.id,
        VISITORS_HEADER.format(days=days_label(days)),
        reply_markup=visitors_keyboard(page, found, now),
    )


async def on_visitor(
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
    days = await runtime_settings.get(session, SettingKey.VISITS_DAYS)
    now = _now()
    since = visits.retention_start(now, days)
    profile = await visits.visitor_profile(session, user_id, callback_data.user, since)
    if profile is None:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    conversation_id = await visits.latest_conversation(session, user_id, profile.user_id)
    blocked = await moderation.is_blocked(session, user_id, profile.user_id)
    notice = await _visit(session, user_id, profile, now)
    await session.commit()
    await query.answer()
    markup = None if conversation_id is None else safety_keyboard(conversation_id, blocked)
    await send_card(bot, avatars, query.from_user.id, profile, VISITOR_HEADER, markup)
    if notice is not None:
        await notice.send(bot)


def create_router() -> Router:
    router = Router(name="people")
    # Private chats only: other users' profiles must never be posted in a group.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_menu, PeopleAction.filter(F.action == "menu"))
    router.callback_query.register(on_past_chats, PeopleAction.filter(F.action == "past"))
    router.callback_query.register(on_open, PeopleAction.filter(F.action == "open"))
    router.callback_query.register(on_visitors, PeopleAction.filter(F.action == "visitors"))
    router.callback_query.register(on_visitor, PeopleAction.filter(F.action == "visitor"))
    return router
