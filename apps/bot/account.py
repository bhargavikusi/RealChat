"""Advanced settings, account deletion, and the access gate (D-016, D-025, D-026).

Advanced settings also holds the notification settings (D-052): Silent mode, visit and
like alerts, and reminders. Each button shows the current value and carries the value
it sets, so a repeated or old tap never flips a setting back. Their buttons have their
own prefix, which the access gate does not let suspended or banned users through.

Deletion is reached from the profile card's Advanced settings button, or from the
notice shown to suspended and banned users. It always needs a second, explicit
confirmation. The confirmation buttons carry a random id kept in FSM state, so a
confirmation button from a cancelled, finished, or older request does nothing.

Ordinary users must have accepted the current terms (D-025); otherwise, even from
an older profile card, they are shown the terms instead. Suspended and banned users
may delete without that, from their notice (D-026).
"""

import logging
import secrets
from datetime import UTC, datetime
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
    TelegramObject,
    Update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.admin import admin_button
from apps.bot.endings import PARTNER_LEFT, coins_label, menu_price, tell_chat_ended
from apps.bot.menu import HelpAction
from apps.bot.middlewares import Handler
from apps.bot.onboarding import load_context, prompt_next
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin import service as admin_service
from backend.modules.chat import service as chat_service
from backend.modules.coins import service as coins
from backend.modules.moderation import service as moderation
from backend.modules.users import service
from backend.modules.users.service import Notification, NotificationChoices

logger = logging.getLogger(__name__)


class AccountAction(CallbackData, prefix="acct"):
    action: str  # "settings", "delete", "confirm", or "keep"
    request: str = ""  # the deletion request a confirm or keep button belongs to


class NotifyAction(CallbackData, prefix="notify"):
    setting: str  # a `Notification` value
    on: bool  # the value this button sets


class BackToProfile(CallbackData, prefix="pback"):
    """◀️ Back from Advanced settings to the profile card (handled in `apps.bot.profile`)."""


class AccountDeletion(StatesGroup):
    confirm = State()


ADVANCED_SETTINGS = "⚙️ Advanced settings"
DELETE_BUTTON = "🗑 Delete account"
CONFIRM_BUTTON = "Yes, delete my account"
KEEP_BUTTON = "Cancel"
ADVANCED_SETTINGS_TEXT = "Advanced settings"
# What deletion removes (D-016, D-055); {coins} is the current balance.
DELETE_CONFIRM = (
    "Delete your account?\n\n"
    "This can't be undone. You'll lose:\n"
    "• Your profile (nickname, age, gender, country, city, bio, and photo) and your "
    "RealChat ID\n"
    "• Your coins (you have {coins})\n"
    "• Your Past chats list, visitors, and settings\n"
    "• Your invite link, and rewards for invites that haven't finished signing up\n\n"
    "A minimal record (your Telegram user ID, account status, and dates) is kept so that "
    "restrictions can't be avoided by signing up again, along with blocks, reports, and "
    "coin records.\n\n"
    "You can come back later with /start and start fresh. One-time rewards you already "
    "received, like the welcome bonus, aren't given again."
)
DELETED = "Your account has been deleted. Send /start if you ever want to come back."
DELETED_RESTRICTED = "Your profile has been erased. Your account remains restricted."
KEPT = "Your account was not deleted."
EXPIRED = "This button has expired."
NOTICES = {
    "suspended": "Your account is suspended, so RealChat is unavailable to you for now.",
    "banned": "Your account has been banned for breaking the rules.",
}
NOTICE_OPTIONS = "You can still read /help, or delete your account."
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


def notification_button(setting: Notification, on: bool) -> InlineKeyboardButton:
    """Shows the current value; a tap sets the other one."""
    return _button(
        f"{NOTIFICATION_LABELS[setting]}: {'On' if on else 'Off'}",
        NotifyAction(setting=setting.value, on=not on),
    )


def settings_keyboard(choices: NotificationChoices, *, admin: bool) -> InlineKeyboardMarkup:
    rows = [[notification_button(setting, choices[setting])] for setting in Notification]
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
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> bool:
    """True for restricted users and users who accepted the current terms.

    Anyone else is shown the current terms, as every other feature does.
    """
    ctx = await load_context(session, query.from_user)
    if service.is_restricted(ctx.user.status) or ctx.terms_accepted:
        return True
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, ctx, state, settings)
    return False


async def show_settings(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if not await _may_delete(query, bot, session, state, settings):
        return
    # The access gate keeps suspended and banned users out of settings (D-026).
    user = await service.get_or_create_user(session, query.from_user.id)
    # Admin settings are offered to admins only; the admin handlers check again (D-035).
    is_admin = await admin_service.get_role(session, query.from_user.id) == "admin"
    choices = await service.notification_settings(session, user.id)
    await session.commit()
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        ADVANCED_SETTINGS_TEXT,
        reply_markup=settings_keyboard(choices, admin=is_admin),
    )


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
    await session.commit()
    await query.answer(changed(setting, callback_data.on))
    if query.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=settings_keyboard(choices, admin=is_admin),
        )
    except TelegramAPIError as exc:
        # The setting is saved either way; Telegram refuses an unchanged keyboard.
        logger.info("could not update settings buttons", extra=exception_summary(exc))


async def ask_to_confirm(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if not await _may_delete(query, bot, session, state, settings):
        return
    user = await service.get_or_create_user(session, query.from_user.id)
    balance = await coins.balance(session, user.id)
    await session.commit()
    request = secrets.token_hex(4)
    await state.set_state(AccountDeletion.confirm)
    await state.set_data({"request": request})
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(CONFIRM_BUTTON, AccountAction(action="confirm", request=request))],
            [_button(KEEP_BUTTON, AccountAction(action="keep", request=request))],
        ]
    )
    await query.answer()
    await bot.send_message(
        query.from_user.id,
        DELETE_CONFIRM.format(coins=coins_label(balance.total)),
        reply_markup=keyboard,
    )


async def _is_current(state: FSMContext, request: str) -> bool:
    data = await state.get_data()
    return (
        await state.get_state() == AccountDeletion.confirm.state
        and bool(request)
        and data.get("request") == request
    )


async def on_confirm(
    query: CallbackQuery,
    bot: Bot,
    callback_data: AccountAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if not await _is_current(state, callback_data.request):
        await query.answer(EXPIRED)
        return
    if not await _may_delete(query, bot, session, state, settings):
        return
    found = await service.get_or_create_user(session, query.from_user.id)
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
    await query.answer()
    await bot.send_message(query.from_user.id, DELETED_RESTRICTED if restricted else DELETED)


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
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(show_settings, AccountAction.filter(F.action == "settings"))
    router.callback_query.register(on_notify, NotifyAction.filter())
    router.callback_query.register(ask_to_confirm, AccountAction.filter(F.action == "delete"))
    router.callback_query.register(on_confirm, AccountAction.filter(F.action == "confirm"))
    router.callback_query.register(on_keep, AccountAction.filter(F.action == "keep"))
    return router


# --- Access gate ---------------------------------------------------------------------


async def _allowed_when_restricted(update: Update, bot: Bot) -> bool:
    """Help and account deletion stay available to restricted users (D-026)."""
    if update.message is not None:
        command = (update.message.text or "").split(maxsplit=1)[:1]
        if not command:
            return False
        name, _, mention = command[0].partition("@")
        if name != "/help":
            return False
        # "/help@name" counts only when it names this bot.
        return not mention or mention.casefold() == ((await bot.me()).username or "").casefold()
    if update.callback_query is not None:
        data = update.callback_query.data or ""
        allowed = (HelpAction.__prefix__, AccountAction.__prefix__)
        return data.split(":", 1)[0] in allowed and data != AccountAction(action="settings").pack()
    return False


class AccessGateMiddleware(BaseMiddleware):
    """Stops suspended and banned users before any handler other than help and deletion.

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
            or await _allowed_when_restricted(event, bot)
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
