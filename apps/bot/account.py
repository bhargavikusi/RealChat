"""Advanced settings, account deletion, and the access gate (D-016, D-025, D-026).

Deletion is reached from the profile card's Advanced settings button, or from the
notice shown to suspended and banned users. It always needs a second, explicit
confirmation. The confirmation buttons carry a random id kept in FSM state, so a
confirmation button from a cancelled, finished, or older request does nothing.

Deleting needs no terms acceptance: a user can always erase their data.
"""

import secrets
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
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

from apps.bot.menu import HelpAction
from apps.bot.middlewares import Handler
from backend.modules.users import service


class AccountAction(CallbackData, prefix="acct"):
    action: str  # "settings", "delete", "confirm", or "keep"
    request: str = ""  # the deletion request a confirm or keep button belongs to


class AccountDeletion(StatesGroup):
    confirm = State()


ADVANCED_SETTINGS = "⚙️ Advanced settings"
DELETE_BUTTON = "🗑 Delete account"
CONFIRM_BUTTON = "Yes, delete my account"
KEEP_BUTTON = "Cancel"
ADVANCED_SETTINGS_TEXT = "Advanced settings"
DELETE_CONFIRM = (
    "Delete your account?\n\n"
    "Your profile (nickname, age, gender, country, city, bio, and photo) is erased "
    "immediately and can't be recovered. A minimal record (your Telegram user ID, "
    "account status, and dates) is kept so that restrictions can't be avoided by "
    "signing up again.\n\n"
    "You can come back later with /start and create a new profile."
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


def _button(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def settings_button() -> InlineKeyboardButton:
    return _button(ADVANCED_SETTINGS, AccountAction(action="settings"))


def _delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(DELETE_BUTTON, AccountAction(action="delete"))]]
    )


def restriction_notice(status: str) -> str:
    return f"{NOTICES[status]}\n\n{NOTICE_OPTIONS}"


async def show_settings(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    await bot.send_message(
        query.from_user.id, ADVANCED_SETTINGS_TEXT, reply_markup=_delete_keyboard()
    )


async def ask_to_confirm(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
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
    await bot.send_message(query.from_user.id, DELETE_CONFIRM, reply_markup=keyboard)


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
) -> None:
    if not await _is_current(state, callback_data.request):
        await query.answer(EXPIRED)
        return
    user = await service.get_or_create_user(session, query.from_user.id)
    restricted = service.is_restricted(user.status)
    await service.delete_account(session, user.id, datetime.now(UTC))
    await session.commit()
    await state.clear()
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
    router.callback_query.register(ask_to_confirm, AccountAction.filter(F.action == "delete"))
    router.callback_query.register(on_confirm, AccountAction.filter(F.action == "confirm"))
    router.callback_query.register(on_keep, AccountAction.filter(F.action == "keep"))
    return router


# --- Access gate ---------------------------------------------------------------------


def _allowed_when_restricted(update: Update) -> bool:
    """Help and account deletion stay available to restricted users (D-026)."""
    if update.message is not None:
        text = update.message.text or ""
        return text.split(maxsplit=1)[:1] == ["/help"] or text.startswith("/help@")
    if update.callback_query is not None:
        data = update.callback_query.data or ""
        allowed = (HelpAction.__prefix__, AccountAction.__prefix__)
        return data.split(":", 1)[0] in allowed and data != AccountAction(action="settings").pack()
    return False


class AccessGateMiddleware(BaseMiddleware):
    """Stops suspended and banned users before any feature handler runs.

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
        if status is None or not service.is_restricted(status) or _allowed_when_restricted(event):
            return await handler(event, data)
        bot: Bot = data["bot"]
        if event.callback_query is not None:
            await event.callback_query.answer()
        chat = data.get("event_chat")
        if chat is not None and chat.type == ChatType.PRIVATE:
            await bot.send_message(
                chat.id, restriction_notice(status), reply_markup=_delete_keyboard()
            )
        return None
