"""People: past chat partners the user can reach again (D-051), visits (D-053), and
opening profiles by RealChat ID (D-054).

👥 People sits on the user's own profile card. It opens 💬 Past chats: everyone the
user chatted with whose chat ended within the window (an admin setting), latest
first, in pages. Tapping someone shows their card with Block (or Unblock) and Report
for that chat (D-045). Blocks hide nobody; the blocker sees 🚫 beside the name.

Every button carries what it lists or opens, and the server checks it again: a
partner button opens a card only while that chat still counts as a past chat, so an
old or forged button opens nobody.

👀 Visited you lists everyone who opened the user's card, once each, latest visit
first. Opening someone's card from People records a visit and tells them, unless they
turned visit alerts off or the two are chatting together (a blocked visitor is
announced too, D-056). A visitor's card opens from the list or from that notice while
the visit is kept. Like the profile, People needs the current terms and a finished
onboarding. The wording and layout are provisional until the design pass. As
elsewhere, every database transaction is ended before any Telegram call.

Every card shows its RealChat ID as /userid_<id>. Sending that, or /userid <id>, opens
the card for anyone, as a visit; /userid alone shows the user's own ID. Unknown IDs
and accounts that can't be opened get the same answer, and wrong lookups are limited
per hour (an admin setting), so IDs can't be found by trying many. A card opened this
way, or from Visited you, has Block and Report for the latest chat the two shared, or
for the profile if they never chatted (D-054).

Every other user's card also has ❤️ Like (or 💔 Unlike) above Block and Report
(`apps.bot.likes`, D-056). ❤️ Liked you and 💘 You liked list current likes, latest
first; opening a card from them is a visit, as elsewhere in People.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.avatars import Avatars
from apps.bot.cards import send_card
from apps.bot.endings import (
    card_keyboard,
    like_row,
    message_row,
    profile_safety_keyboard,
    request_row,
    safety_keyboard,
)
from apps.bot.onboarding import Context, load_context, prompt_next
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.moderation import service as moderation
from backend.modules.people import likes, visits
from backend.modules.people import service as people
from backend.modules.people.lookups import IdLookups, LookupsUnavailable
from backend.modules.users import public_ids
from backend.modules.users import service as users
from backend.modules.users.models import Profile

logger = logging.getLogger(__name__)


class PeopleAction(CallbackData, prefix="people"):
    # "menu", "past" (a page of past chats), "open" (a past partner's card), "visitors"
    # (a page of Visited you), "visitor" (a visitor's card), "likers" (a page of Liked
    # you), "liker" (a liker's card), "liking" (a page of You liked), or "liked" (the
    # card of someone the user likes)
    action: str
    page: int = 0
    conversation: int = 0  # the past chat an "open" button belongs to
    user: int = 0  # the person a "visitor", "liker", or "liked" button opens


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
LIKERS_BUTTON = "❤️ Liked you"
LIKERS_HEADER = "❤️ Liked you"
NO_LIKERS = "Nobody has liked your profile yet."
LIKER_HEADER = "❤️ Liked your profile"
LIKING_BUTTON = "💘 You liked"
LIKING_HEADER = "💘 You liked"
NO_LIKING = "You haven't liked anyone yet."
LIKED_HEADER = "💘 You liked this profile"
BLOCKED_MARK = " 🚫"
OWN_ID = (
    "🆔 Your RealChat ID: /userid_{id}\n\n"
    "Anyone who has it can open your profile. To open someone else's, send /userid "
    "followed by their ID."
)
ID_HEADER = "🆔 RealChat profile"
OWN_CARD_HEADER = "🆔 This is your profile."
NO_PROFILE = "No profile with that ID."
LOOKUPS_PAUSED = "Too many wrong IDs. Please try again later."
LOOKUPS_UNAVAILABLE = "Profiles can't be looked up right now. Please try again later."
# /userid_<id>, as shown on every card, in any letter case. The whole command is taken,
# so `public_ids.normalize` refuses anything after the ID instead of ignoring it.
ID_LINK = re.compile(r"userid_(\S+)$", re.IGNORECASE)


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


def _like_entry(entry: likes.LikeEntry, action: str, now: datetime) -> InlineKeyboardButton:
    mark = BLOCKED_MARK if entry.blocked else ""
    return _button(
        f"{entry.nickname} · {ago(entry.liked_at, now)}{mark}",
        PeopleAction(action=action, user=entry.user_id),
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


def likes_keyboard(
    list_action: str, entry_action: str, page: int, found: likes.LikeEntries, now: datetime
) -> InlineKeyboardMarkup:
    rows = [[_like_entry(entry, entry_action, now)] for entry in found.entries]
    return _pages(rows, list_action, page, found.has_next)


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
    if not await visits.record_visit(session, visitor_id, visited.user_id, now):
        return None
    if not await visits.should_notify(session, visitor_id, visited.user_id):
        return None
    nickname = (await users.get_profile(session, visitor_id)).nickname
    chat_id = (await users.get_user(session, visited.user_id)).telegram_user_id
    return Notice(chat_id, visitor_id, str(nickname))


async def _ready_context(
    bot: Bot,
    telegram_user: TelegramUser,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> Context | None:
    """The user's context once they have accepted the current terms and finished
    onboarding. Otherwise they are shown the terms or their onboarding step, and None
    is returned."""
    ctx = await load_context(session, telegram_user)
    if ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None:
        return ctx
    await prompt_next(bot, telegram_user.id, session, ctx, state, settings)
    return None


async def ready_user(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> int | None:
    """As `_ready_context`, for a button: the user's id, or None."""
    ctx = await load_context(session, query.from_user)
    if ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None:
        return ctx.user.id
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, ctx, state, settings)
    return None


async def _card_rows(
    session: AsyncSession, user_id: int, other: Profile, runtime_settings: RuntimeSettings
) -> list[list[InlineKeyboardButton]]:
    """Like (or Unlike), and ✉️ Message (D-057) and 💬 Chat request (D-058) unless the
    card's owner blocked the user or the two are chatting now. The services check again
    when they are tapped."""
    rows = [like_row(other.public_id, await likes.is_liked(session, user_id, other.user_id))]
    active = await chat.active_chat(session, user_id)
    chatting = active is not None and active.partner_id == other.user_id
    if not chatting and not await moderation.is_blocked(session, other.user_id, user_id):
        price = await runtime_settings.get(session, SettingKey.DIRECT_MESSAGE_COINS)
        rows.append(message_row(other.public_id, price))
        # Offered whatever the person is doing; the tap says if they can't take one now,
        # so the card never shows their activity (D-058).
        request_price = await runtime_settings.get(session, SettingKey.CHAT_REQUEST_COINS)
        rows.append(request_row(other.public_id, request_price))
    return rows


async def _card_buttons(
    session: AsyncSession, user_id: int, other: Profile, runtime_settings: RuntimeSettings
) -> InlineKeyboardMarkup:
    """Like and Message, then Block (or Unblock) and Report on another user's card: for
    the latest chat the two shared, or for the profile if they never chatted (D-054)."""
    blocked = await moderation.is_blocked(session, user_id, other.user_id)
    conversation_id = await visits.latest_conversation(session, user_id, other.user_id)
    safety = (
        profile_safety_keyboard(other.public_id, blocked)
        if conversation_id is None
        else safety_keyboard(conversation_id, blocked)
    )
    return card_keyboard(await _card_rows(session, user_id, other, runtime_settings), safety)


async def on_menu(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if await ready_user(query, bot, session, state, settings) is None:
        return
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        PEOPLE_PROMPT,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_button(PAST_CHATS_BUTTON, PeopleAction(action="past"))],
                [_button(VISITORS_BUTTON, PeopleAction(action="visitors"))],
                [_button(LIKERS_BUTTON, PeopleAction(action="likers"))],
                [_button(LIKING_BUTTON, PeopleAction(action="liking"))],
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
    user_id = await ready_user(query, bot, session, state, settings)
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
    user_id = await ready_user(query, bot, session, state, settings)
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
    markup = card_keyboard(
        await _card_rows(session, user_id, profile, runtime_settings),
        safety_keyboard(conversation_id, blocked),
    )
    notice = await _visit(session, user_id, profile, _now())
    await session.commit()
    await query.answer()
    await send_card(bot, avatars, query.from_user.id, profile, PAST_CHAT_HEADER, markup)
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
    user_id = await ready_user(query, bot, session, state, settings)
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
    user_id = await ready_user(query, bot, session, state, settings)
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
    markup = await _card_buttons(session, user_id, profile, runtime_settings)
    notice = await _visit(session, user_id, profile, now)
    await session.commit()
    await query.answer()
    await send_card(bot, avatars, query.from_user.id, profile, VISITOR_HEADER, markup)
    if notice is not None:
        await notice.send(bot)


LikeList = Callable[[AsyncSession, int, int], Awaitable[likes.LikeEntries]]
LikedProfile = Callable[[AsyncSession, int, int], Awaitable[Profile | None]]


async def _show_likes(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    *,
    fetch: LikeList,
    header: str,
    empty: str,
    entry_action: str,
) -> None:
    """A page of Liked you or You liked."""
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    page = max(callback_data.page, 0)
    found = await fetch(session, user_id, page)
    await session.commit()
    await query.answer()
    if not found.entries and page == 0:
        await bot.send_message(query.from_user.id, empty)
        return
    await bot.send_message(
        query.from_user.id,
        header,
        reply_markup=likes_keyboard(callback_data.action, entry_action, page, found, _now()),
    )


async def _open_like(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    avatars: Avatars,
    *,
    find: LikedProfile,
    header: str,
) -> None:
    """The card of someone in Liked you or You liked, while the like stands. A visit."""
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    profile = await find(session, user_id, callback_data.user)
    if profile is None:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    markup = await _card_buttons(session, user_id, profile, runtime_settings)
    notice = await _visit(session, user_id, profile, _now())
    await session.commit()
    await query.answer()
    await send_card(bot, avatars, query.from_user.id, profile, header, markup)
    if notice is not None:
        await notice.send(bot)


async def on_likers(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    await _show_likes(
        query,
        bot,
        callback_data,
        session,
        state,
        settings,
        fetch=likes.likers,
        header=LIKERS_HEADER,
        empty=NO_LIKERS,
        entry_action="liker",
    )


async def on_liker(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    avatars: Avatars,
) -> None:
    await _open_like(
        query,
        bot,
        callback_data,
        session,
        state,
        settings,
        runtime_settings,
        avatars,
        find=likes.liker_profile,
        header=LIKER_HEADER,
    )


async def on_liking(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    await _show_likes(
        query,
        bot,
        callback_data,
        session,
        state,
        settings,
        fetch=likes.liked,
        header=LIKING_HEADER,
        empty=NO_LIKING,
        entry_action="liked",
    )


async def on_liked(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PeopleAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    avatars: Avatars,
) -> None:
    await _open_like(
        query,
        bot,
        callback_data,
        session,
        state,
        settings,
        runtime_settings,
        avatars,
        find=likes.liked_profile,
        header=LIKED_HEADER,
    )


async def on_userid(
    message: Message,
    bot: Bot,
    command: CommandObject,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    avatars: Avatars,
    id_lookups: IdLookups,
) -> None:
    """/userid alone shows the user's own ID; /userid <id> and /userid_<id> open that
    profile."""
    raw = command.args
    if command.regexp_match is not None:
        # Text after /userid_<id> is kept, so it makes the ID invalid rather than ignored.
        raw = " ".join(part for part in (command.regexp_match.group(1), command.args) if part)
    if message.from_user is None:
        return
    ctx = await _ready_context(bot, message.from_user, session, state, settings)
    if ctx is None:
        return
    own = ctx.profile
    if raw is None or not raw.strip():
        await message.answer(OWN_ID.format(id=own.public_id))
        return
    public_id = public_ids.normalize(raw)
    if public_id == own.public_id:
        await send_card(bot, avatars, message.chat.id, own, OWN_CARD_HEADER)
        return
    limit = await runtime_settings.get(session, SettingKey.USERID_MISSES_PER_HOUR)
    try:
        if await id_lookups.paused(ctx.user.id, limit):
            await session.commit()
            await message.answer(LOOKUPS_PAUSED)
            return
        profile = None if public_id is None else await users.find_profile(session, public_id)
        if profile is None:
            await session.commit()
            await id_lookups.miss(ctx.user.id)
            await message.answer(NO_PROFILE)
            return
    except LookupsUnavailable:
        await session.rollback()
        logger.warning("RealChat ID lookups unavailable")
        await message.answer(LOOKUPS_UNAVAILABLE)
        return
    markup = await _card_buttons(session, ctx.user.id, profile, runtime_settings)
    notice = await _visit(session, ctx.user.id, profile, _now())
    await session.commit()
    await send_card(bot, avatars, message.chat.id, profile, ID_HEADER, markup)
    if notice is not None:
        await notice.send(bot)


def create_router() -> Router:
    router = Router(name="people")
    # Private chats only: other users' profiles must never be posted in a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.message.register(on_userid, Command("userid", ID_LINK, ignore_case=True))
    router.callback_query.register(on_menu, PeopleAction.filter(F.action == "menu"))
    router.callback_query.register(on_past_chats, PeopleAction.filter(F.action == "past"))
    router.callback_query.register(on_open, PeopleAction.filter(F.action == "open"))
    router.callback_query.register(on_visitors, PeopleAction.filter(F.action == "visitors"))
    router.callback_query.register(on_visitor, PeopleAction.filter(F.action == "visitor"))
    router.callback_query.register(on_likers, PeopleAction.filter(F.action == "likers"))
    router.callback_query.register(on_liker, PeopleAction.filter(F.action == "liker"))
    router.callback_query.register(on_liking, PeopleAction.filter(F.action == "liking"))
    router.callback_query.register(on_liked, PeopleAction.filter(F.action == "liked"))
    return router
