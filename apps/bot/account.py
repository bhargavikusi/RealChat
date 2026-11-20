"""Advanced settings, account deletion, and the access gate (D-016, D-025, D-026).

Advanced settings also holds the notification settings (D-052): Silent mode, visit and
like alerts, and reminders; and, for VIP users only, Ghost mode (D-064). Each button
shows the current value and carries the value it sets, so a repeated or old tap never
flips a setting back. Their buttons have their own prefix, which the access gate does
not let suspended or banned users through.

Deletion is reached from the profile card's Advanced settings button, or from the
notice shown to suspended and banned users. After the warning, the user must type
exactly DELETE (D-067); any other reply keeps the account and ends the request. The
request waits in FSM state, and its Cancel button carries a random id kept there, so a
Cancel button from a cancelled, finished, or older request does nothing.

Ordinary users must have accepted the current terms (D-025); otherwise, even from
an older profile card, they are shown the terms instead. Suspended and banned users
may delete without that, from their notice (D-026).
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    Update,
)
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.admin import admin_button
from apps.bot.endings import PARTNER_LEFT, coins_label, menu_price, tell_chat_ended
from apps.bot.legal import LegalAction
from apps.bot.menu import HelpAction
from apps.bot.middlewares import Handler, is_payment
from apps.bot.onboarding import load_context, prompt_next
from apps.bot.payments import vip_date
from apps.bot.people import days_label
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin import service as admin_service
from backend.modules.chat import service as chat_service
from backend.modules.coins import service as coins
from backend.modules.moderation import service as moderation
from backend.modules.payments import service as payments
from backend.modules.users import service
from backend.modules.users.models import User
from backend.modules.users.service import Notification, NotificationChoices

logger = logging.getLogger(__name__)


class AccountAction(CallbackData, prefix="acct"):
    action: str  # "settings", "delete", "confirm", or "keep"
    request: str = ""  # the deletion request a confirm or keep button belongs to


class NotifyAction(CallbackData, prefix="notify"):
    setting: str  # a `Notification` value
    on: bool  # the value this button sets


class GhostAction(CallbackData, prefix="ghost"):
    """👻 Ghost mode, a VIP setting (D-064)."""

    on: bool  # the value this button sets


class BackToProfile(CallbackData, prefix="pback"):
    """◀️ Back from Advanced settings to the profile card (handled in `apps.bot.profile`)."""


class AccountDeletion(StatesGroup):
    confirm = State()


ADVANCED_SETTINGS = "⚙️ Advanced settings"
DELETE_BUTTON = "🗑 Delete account"
# The reply that confirms a deletion, exactly (D-067).
CONFIRM_WORD = "DELETE"
KEEP_BUTTON = "Cancel"
ADVANCED_SETTINGS_TEXT = "Advanced settings"
# What deletion removes (D-016, D-055, D-062, D-067); {coins} is the current balance,
# and {vip} is VIP_LOST while VIP is running, or empty.
DELETE_CONFIRM = (
    "Delete your account?\n\n"
    "This can't be undone. You'll lose:\n"
    "• Your profile (nickname, age, gender, country, city, bio, and photo) and your "
    "RealChat ID\n"
    "• Your coins (you have {coins}), including any you bought. They aren't refunded.\n"
    "{vip}"
    "• Your Past chats list, visitors, and settings\n"
    "• Your invite link, and rewards for invites that haven't finished signing up\n\n"
    "A minimal record (your Telegram user ID, account status, and dates) is kept so that "
    "restrictions can't be avoided by signing up again, along with blocks, reports, and "
    "coin records.\n\n"
    "You can come back later with /start and start fresh. One-time rewards you already "
    "received, like the welcome bonus, aren't given again.\n\n"
    f"To delete your account, reply with {CONFIRM_WORD} in capital letters. Any other "
    "reply, including in a chat, keeps your account."
)
VIP_LOST = "• Your VIP, with {days} left (until {until}). It isn't refunded.\n"
NOT_CONFIRMED = (
    f"Your account was not deleted. To delete it, tap 🗑 Delete account again and reply "
    f"with exactly {CONFIRM_WORD}."
)
DELETED = "Your account has been deleted. Send /start if you ever want to come back."
DELETED_RESTRICTED = "Your profile has been erased. Your account remains restricted."
KEPT = "Your account was not deleted."
EXPIRED = "This button has expired."
NOTICES = {
    "suspended": "Your account is suspended, so RealChat is unavailable to you for now.",
    "banned": "Your account has been banned for breaking the rules.",
}
NOTICE_OPTIONS = (
    "You can still read /help and the /terms, get help with a purchase with /paysupport, "
    "or delete your account."
)
BACK_BUTTON = "◀️ Back"
NOTIFICATION_LABELS = {
    Notification.SILENT_MODE: "🤫 Silent mode",
    Notification.VISIT_ALERTS: "👀 Visit alerts",
    Notification.LIKE_ALERTS: "❤️ Like alerts",
    Notification.REMINDERS: "⏰ Reminders",
    Notification.ACTIVITY_ALERTS: "🔔 Activity alerts about me",
}
SILENT_MODE_ON = "Silent mode is on: nobody can send you chat requests. Messages still arrive."
SILENT_MODE_OFF = "Silent mode is off."
GHOST_LABEL = "👻 Ghost mode"
GHOST_ON = "👻 Ghost mode is on: profiles you open won't see your visit."
GHOST_OFF = "👻 Ghost mode is off: profiles you open will see your visit again."
GHOST_VIP_ONLY = "👻 Ghost mode is a VIP perk. See /vip."


def notification_button(setting: Notification, on: bool) -> InlineKeyboardButton:
    """Shows the current value; a tap sets the other one."""
    return _button(
        f"{NOTIFICATION_LABELS[setting]}: {'On' if on else 'Off'}",
        NotifyAction(setting=setting.value, on=not on),
    )


def settings_keyboard(
    choices: NotificationChoices, *, admin: bool, ghost: bool | None = None
) -> InlineKeyboardMarkup:
    """`ghost` is the Ghost mode setting for a VIP, or None to leave its row out."""
    rows = [[notification_button(setting, choices[setting])] for setting in Notification]
    if ghost is not None:
        rows.append(
            [_button(f"{GHOST_LABEL}: {'On' if ghost else 'Off'}", GhostAction(on=not ghost))]
        )
    rows.append([_button(DELETE_BUTTON, AccountAction(action="delete"))])
    if admin:
        rows.append([admin_button()])
    rows.append([_button(BACK_BUTTON, BackToProfile())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def changed(setting: Notification, on: bool) -> str:
    if setting is Notification.SILENT_MODE:
        return SILENT_MODE_ON if on else SILENT_MODE_OFF
    return f"{NOTIFICATION_LABELS[setting]}: {'On' if on else 'Off'}"


def _button(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def settings_button() -> InlineKeyboardButton:
    return _button(ADVANCED_SETTINGS, AccountAction(action="settings"))


def _delete_keyboard(*, admin: bool = False) -> InlineKeyboardMarkup:
    rows = [[_button(DELETE_BUTTON, AccountAction(action="delete"))]]
    if admin:
        rows.append([admin_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def restriction_notice(status: str) -> str:
    return f"{NOTICES[status]}\n\n{NOTICE_OPTIONS}"


async def _may_delete(
    telegram_user: TelegramUser,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    query: CallbackQuery | None = None,
) -> bool:
    """True for restricted users and users who accepted the current terms.

    Anyone else is shown the current terms, as every other feature does.
    """
    ctx = await load_context(session, telegram_user)
    if service.is_restricted(ctx.user.status) or ctx.terms_accepted:
        return True
    if query is not None:
        await query.answer()
    await prompt_next(bot, telegram_user.id, session, ctx, state, settings)
    return False


async def show_settings(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if not await _may_delete(query.from_user, bot, session, state, settings, query):
        return
    # The access gate keeps suspended and banned users out of settings (D-026).
    user = await service.get_or_create_user(session, query.from_user.id)
    # Admin settings are offered to admins only; the admin handlers check again (D-035).
    is_admin = await admin_service.get_role(session, query.from_user.id) == "admin"
    choices = await service.notification_settings(session, user.id)
    ghost = _ghost(user)
    await session.commit()
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        ADVANCED_SETTINGS_TEXT,
        reply_markup=settings_keyboard(choices, admin=is_admin, ghost=ghost),
    )


def _ghost(user: User) -> bool | None:
    """The Ghost mode row's value: the setting while VIP, else None (no row)."""
    return user.ghost_mode if payments.is_vip(user, datetime.now(UTC)) else None


async def _refresh_settings(bot: Bot, query: CallbackQuery, keyboard: InlineKeyboardMarkup) -> None:
    if query.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=keyboard,
        )
    except TelegramAPIError as exc:
        # The setting is saved either way; Telegram refuses an unchanged keyboard.
        logger.info("could not update settings buttons", extra=exception_summary(exc))


async def on_notify(
    query: CallbackQuery,
    bot: Bot,
    callback_data: NotifyAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    try:
        setting = Notification(callback_data.setting)
    except ValueError:
        await query.answer(EXPIRED)
        return
    ctx = await load_context(session, query.from_user)
    if not ctx.terms_accepted:
        await query.answer()
        await prompt_next(bot, query.from_user.id, session, ctx, state, settings)
        return
    await service.set_notification(session, ctx.user.id, setting, callback_data.on)
    is_admin = ctx.user.role == "admin"
    choices = await service.notification_settings(session, ctx.user.id)
    ghost = _ghost(ctx.user)
    await session.commit()
    await query.answer(changed(setting, callback_data.on))
    await _refresh_settings(bot, query, settings_keyboard(choices, admin=is_admin, ghost=ghost))


async def on_ghost(
    query: CallbackQuery,
    bot: Bot,
    callback_data: GhostAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    ctx = await load_context(session, query.from_user)
    if not ctx.terms_accepted:
        await query.answer()
        await prompt_next(bot, query.from_user.id, session, ctx, state, settings)
        return
    # The server decides who is VIP; an old button from a past VIP changes nothing. The
    # ORM update also refreshes `ctx.user.ghost_mode` in this session.
    done = await payments.set_ghost_mode(session, ctx.user.id, callback_data.on, datetime.now(UTC))
    is_admin = ctx.user.role == "admin"
    choices = await service.notification_settings(session, ctx.user.id)
    ghost = _ghost(ctx.user)
    await session.commit()
    if not done:
        await query.answer(GHOST_VIP_ONLY, show_alert=True)
    else:
        await query.answer(GHOST_ON if callback_data.on else GHOST_OFF)
    await _refresh_settings(bot, query, settings_keyboard(choices, admin=is_admin, ghost=ghost))


async def ask_to_confirm(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if not await _may_delete(query.from_user, bot, session, state, settings, query):
        return
    user = await service.get_or_create_user(session, query.from_user.id)
    balance = await coins.balance(session, user.id)
    await session.commit()
    now = datetime.now(UTC)
    request = secrets.token_hex(4)
    await state.set_state(AccountDeletion.confirm)
    await state.set_data({"request": request})
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[_button(KEEP_BUTTON, AccountAction(action="keep", request=request))]]
    )
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        DELETE_CONFIRM.format(
            coins=coins_label(balance.total),
            vip=VIP_LOST.format(
                days=days_label(vip_days_left(user.vip_until, now)), until=vip_date(user.vip_until)
            )
            if user.vip_until is not None and payments.is_vip(user, now)
            else "",
        ),
        reply_markup=keyboard,
    )


def vip_days_left(until: datetime, now: datetime) -> int:
    """Whole days of VIP left, counting a part day as one."""
    return -(-(until - now) // timedelta(days=1))


async def _is_current(state: FSMContext, request: str) -> bool:
    data = await state.get_data()
    return (
        await state.get_state() == AccountDeletion.confirm.state
        and bool(request)
        and data.get("request") == request
    )


async def on_typed(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    """The reply to the deletion warning: exactly CONFIRM_WORD deletes, anything else
    keeps the account and ends the request (D-067)."""
    if message.from_user is None:
        return
    if message.text != CONFIRM_WORD:
        await state.clear()
        await message.answer(NOT_CONFIRMED)
        return
    if not await _may_delete(message.from_user, bot, session, state, settings):
        return
    found = await service.get_or_create_user(session, message.from_user.id)
    # The row is locked before anything else, so a match being created for this user
    # either finishes first (and is ended below) or waits and then finds the account
    # deleted.
    user = await service.lock_user(session, found.id)
    restricted = service.is_restricted(user.status)
    now = datetime.now(UTC)
    # A deleted account leaves any chat it is in, so the partner is not left waiting.
    ended = await chat_service.end_conversation(session, user.id, now)
    partner = None if ended is None else await service.get_user(session, ended.partner_id)
    partner_blocked = partner is not None and await moderation.is_blocked(
        session, partner.id, user.id
    )
    await service.delete_account(session, user.id, now)
    price = await menu_price(session)
    await session.commit()
    await state.clear()
    # The partner is told first, so a failure replying to this user cannot skip it.
    if ended is not None and partner is not None:
        await tell_chat_ended(
            bot,
            partner.telegram_user_id,
            PARTNER_LEFT,
            ended.conversation_id,
            blocked=partner_blocked,
            price=price,
        )
    await message.answer(DELETED_RESTRICTED if restricted else DELETED)


async def on_old_confirm(query: CallbackQuery) -> None:
    """The one-tap confirm button from before typed confirmation (D-067)."""
    await query.answer(EXPIRED)


async def on_keep(
    query: CallbackQuery, bot: Bot, callback_data: AccountAction, state: FSMContext
) -> None:
    if not await _is_current(state, callback_data.request):
        await query.answer(EXPIRED)
        return
    await state.clear()
    await query.answer()
    await bot.send_message(query.from_user.id, KEPT)


def create_router() -> Router:
    router = Router(name="account")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(show_settings, AccountAction.filter(F.action == "settings"))
    router.callback_query.register(on_notify, NotifyAction.filter())
    router.callback_query.register(on_ghost, GhostAction.filter())
    router.callback_query.register(ask_to_confirm, AccountAction.filter(F.action == "delete"))
    router.callback_query.register(on_old_confirm, AccountAction.filter(F.action == "confirm"))
    router.callback_query.register(on_keep, AccountAction.filter(F.action == "keep"))
    # Any message while the warning waits for its reply, before the chat relays it.
    router.message.register(on_typed, AccountDeletion.confirm)
    return router


# --- Access gate ---------------------------------------------------------------------


async def _allowed_when_restricted(update: Update, bot: Bot, raw_state: str | None) -> bool:
    """Help, the terms, payment support, and account deletion stay available to
    restricted users (D-026, D-061). Payment updates pass too: checkout declines
    restricted users itself, with a reason, and a confirmed payment must be recorded.
    Any reply to a deletion warning, text or media, passes while the warning waits for it
    (D-067), but no command does: a command would reach handlers before the deletion
    check."""
    if is_payment(update):
        return True
    if update.message is not None:
        # Command filters match a caption too, so a captioned "/vip" is a command.
        text = update.message.text or update.message.caption or ""
        if raw_state == AccountDeletion.confirm.state and not text.startswith("/"):
            return True
        command = text.split(maxsplit=1)[:1]
        if not command:
            return False
        name, _, mention = command[0].partition("@")
        if name not in ("/help", "/terms", "/paysupport"):
            return False
        # "/help@name" counts only when it names this bot.
        return not mention or mention.casefold() == ((await bot.me()).username or "").casefold()
    if update.callback_query is not None:
        data = update.callback_query.data or ""
        allowed = (HelpAction.__prefix__, LegalAction.__prefix__, AccountAction.__prefix__)
        return data.split(":", 1)[0] in allowed and data != AccountAction(action="settings").pack()
    return False


class AccessGateMiddleware(BaseMiddleware):
    """Stops suspended and banned users before any handler other than help, the terms,
    payment support, payment updates, and deletion (`_allowed_when_restricted`).

    Registered as an inner update middleware after the database session, so it runs
    for every update that passed deduplication and flood control. The status lookup
    is committed before any reply, so no transaction is held across a Telegram call.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if user is None or session is None or not isinstance(event, Update):
            return await handler(event, data)
        status = await service.get_status(session, user.id)
        await session.commit()
        bot: Bot = data["bot"]
        if (
            status is None
            or not service.is_restricted(status)
            or await _allowed_when_restricted(event, bot, data.get("raw_state"))
        ):
            return await handler(event, data)
        if event.callback_query is not None:
            await event.callback_query.answer()
        chat = data.get("event_chat")
        if chat is not None and chat.type == ChatType.PRIVATE:
            await bot.send_message(
                chat.id, restriction_notice(status), reply_markup=_delete_keyboard()
            )
        return None
