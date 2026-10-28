"""🛠 Admin settings: runtime settings for admins, inside Advanced settings (D-035).

The button is shown only to admins, but that is a convenience, not the check: every
handler here loads the account and requires the admin role and the current terms
before showing or changing anything. Anyone else pressing an admin button (an old
message after the role was revoked, or hand-made callback data) gets "expired" and
nothing changes. Roles themselves are never changed from the bot (D-034).

Each edit gets a random id kept in FSM state and carried by its Cancel button, as
in profile editing, so a Cancel button from an older edit does nothing.
"""

import secrets

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.onboarding import load_context
from backend.modules.admin import service
from backend.modules.admin.service import AdminError
from backend.modules.admin.settings import SPECS, RuntimeSettings, SettingKey, load_values


class AdminAction(CallbackData, prefix="adm"):
    action: str  # "open", "edit", or "cancel"
    key: str = ""  # the setting an "edit" button opens
    edit: str = ""  # the edit a "cancel" button belongs to


class AdminSettingsEdit(StatesGroup):
    value = State()


ADMIN_BUTTON = "🛠 Admin settings"
ADMIN_HEADER = "🛠 Admin settings"
ADMIN_FOOTER = "Tap a setting to change it. Changes apply within a few seconds and are logged."
CANCEL_BUTTON = "Cancel"
CANCELLED = "Cancelled. Nothing was changed."
EXPIRED = "This button has expired."
NOT_AVAILABLE = "Admin settings are not available."
NUMBER_EXPECTED = "Please send a number, or tap Cancel."


def _button(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def admin_button() -> InlineKeyboardButton:
    return _button(ADMIN_BUTTON, AdminAction(action="open"))


def _describe(key: SettingKey, value: int) -> str:
    spec = SPECS[key]
    note = f" ({spec.note})" if spec.note else ""
    return f"{spec.label}: {value} {spec.unit}{note}"


def overview(values: dict[SettingKey, int]) -> str:
    lines = "\n".join(_describe(key, value) for key, value in values.items())
    return f"{ADMIN_HEADER}\n\n{lines}\n\n{ADMIN_FOOTER}"


def _overview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(spec.label, AdminAction(action="edit", key=key.value))]
            for key, spec in SPECS.items()
        ]
    )


def edit_prompt(key: SettingKey, value: int) -> str:
    spec = SPECS[key]
    note = f" {spec.note[0].upper()}{spec.note[1:]}." if spec.note else ""
    return (
        f"{spec.label}\nCurrent value: {value} {spec.unit}.\n\n"
        f"Send a new value from {spec.minimum} to {spec.maximum} ({spec.unit}).{note}"
    )


def _cancel_keyboard(edit: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(CANCEL_BUTTON, AdminAction(action="cancel", edit=edit))]]
    )


async def _admin_id(session: AsyncSession, telegram_user: User) -> int | None:
    """The account id if this user is an admin who accepted the current terms.

    The transaction is ended before returning, so no Telegram call runs inside it.
    """
    ctx = await load_context(session, telegram_user)
    if ctx.user.role == "admin" and ctx.terms_accepted:
        return ctx.user.id
    return None


async def _values(session: AsyncSession) -> dict[SettingKey, int]:
    values = await load_values(session)
    await session.commit()
    return values


async def _editing(state: FSMContext, edit: str) -> SettingKey | None:
    """The setting being edited, if `edit` is the current edit's id."""
    data = await state.get_data()
    if await state.get_state() != AdminSettingsEdit.value.state or not edit:
        return None
    if data.get("edit") != edit:
        return None
    return SettingKey(data["key"])


async def show_overview(query: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if await _admin_id(session, query.from_user) is None:
        await query.answer(EXPIRED)
        return
    values = await _values(session)
    await query.answer()
    await bot.send_message(query.from_user.id, overview(values), reply_markup=_overview_keyboard())


async def on_edit(
    query: CallbackQuery,
    bot: Bot,
    callback_data: AdminAction,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if await _admin_id(session, query.from_user) is None or callback_data.key not in SPECS:
        await query.answer(EXPIRED)
        return
    key = SettingKey(callback_data.key)
    values = await _values(session)
    edit = secrets.token_hex(4)
    await state.set_state(AdminSettingsEdit.value)
    await state.set_data({"edit": edit, "key": key.value})
    await query.answer()
    await bot.send_message(
        query.from_user.id, edit_prompt(key, values[key]), reply_markup=_cancel_keyboard(edit)
    )


async def on_cancel(
    query: CallbackQuery, bot: Bot, callback_data: AdminAction, state: FSMContext
) -> None:
    if await _editing(state, callback_data.edit) is None:
        await query.answer(EXPIRED)
        return
    await state.clear()
    await query.answer()
    await bot.send_message(query.from_user.id, CANCELLED)


async def on_value(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    runtime_settings: RuntimeSettings,
) -> None:
    if message.from_user is None or message.text is None:
        return
    data = await state.get_data()
    actor = await _admin_id(session, message.from_user)
    if actor is None:
        # The role was revoked (or the terms changed) during the edit.
        await state.clear()
        await message.answer(NOT_AVAILABLE)
        return
    key = SettingKey(data["key"])
    edit = str(data.get("edit", ""))
    try:
        value = service.parse_setting_value(key, message.text)
        old = await service.change_setting(session, actor, key, value)
    except AdminError as error:
        await session.rollback()
        await message.answer(error.message, reply_markup=_cancel_keyboard(edit))
        return
    await session.commit()
    runtime_settings.invalidate()
    await state.clear()
    spec = SPECS[key]
    saved = (
        f"✅ {spec.label} is now {value} {spec.unit} (was {old})."
        if old != value
        else f"{spec.label} is already {value} {spec.unit}. Nothing changed."
    )
    values = await _values(session)
    await message.answer(f"{saved}\n\n{overview(values)}", reply_markup=_overview_keyboard())


async def number_expected(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer(NUMBER_EXPECTED, reply_markup=_cancel_keyboard(str(data.get("edit", ""))))


def create_router() -> Router:
    router = Router(name="admin")
    # Private chats only: settings and their values are never posted in a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(show_overview, AdminAction.filter(F.action == "open"))
    router.callback_query.register(on_edit, AdminAction.filter(F.action == "edit"))
    router.callback_query.register(on_cancel, AdminAction.filter(F.action == "cancel"))
    # Commands are never taken as values; they reach their own handlers.
    not_a_command = ~F.text.startswith("/")
    router.message.register(on_value, AdminSettingsEdit.value, F.text, not_a_command)
    router.message.register(number_expected, AdminSettingsEdit.value, not_a_command)
    return router
