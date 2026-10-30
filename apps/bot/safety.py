"""Blocking and reporting chat partners (D-037, D-045).

🚫 Block and ⚠️ Report sit on the partner's profile card and on the message shown
after a chat ends. Every button carries the conversation it belongs to; the service
checks that the user was in it, so a forged or stale button can only act on that
chat's partner, never on anyone else.

* Block toggles with ✅ Unblock on the same button. Blocking someone you are still
  chatting with asks first, because it ends the chat; otherwise it takes one tap.
* Report asks for a reason and files the report at once. It also blocks the partner
  and ends the chat if it is still open. Optional details can then be added.
* The partner is only told that the chat ended, never that they were blocked or
  reported.

As elsewhere, every database transaction is ended before any Telegram call.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.chat import tell_partner_ended
from apps.bot.endings import SafetyAction, safety_keyboard, send_chat_ended, send_menu
from apps.bot.keyboards import main_keyboard
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.matching.queue import SearchQueue
from backend.modules.moderation import service as moderation
from backend.modules.moderation.models import REPORT_DETAILS_MAX_LENGTH, REPORT_REASONS
from backend.modules.moderation.service import NotPartnerError, ReportOutcome
from backend.modules.users import service as users

logger = logging.getLogger(__name__)


class ReportDetails(StatesGroup):
    text = State()


NOT_AVAILABLE = "This button isn't available."
CONFIRM_BLOCK = "Block your chat partner? Blocking will end this chat."
CONFIRM_YES = "🚫 Yes, block"
CONFIRM_NO = "Cancel"
NOT_BLOCKED = "Not blocked."
BLOCKED = "Blocked."
ALREADY_BLOCKED = "Already blocked."
UNBLOCKED = "Unblocked."
YOU_BLOCKED = "You blocked your chat partner, so the chat has ended."

REPORT_PROMPT = "Why are you reporting this person?"
REASON_LABELS = {
    "spam": "Spam or scam",
    "harassment": "Harassment or hate",
    "sexual": "Sexual content without consent",
    "under_18": "Appears under 18",
    "threats": "Threats or violence",
    "other": "Other",
}
ALREADY_REPORTED = "You've already reported this chat."
LIMIT_REACHED = (
    "You've sent a lot of reports recently, so this one wasn't sent. Please try again "
    "later. You can still block people."
)
REPORT_SENT = "Thanks, your report was sent, and this person is blocked."
REPORT_SENT_ENDED = "Thanks, your report was sent. This person is blocked and the chat has ended."
DETAILS_PROMPT = (
    f"If you like, send a short description of what happened (up to "
    f"{REPORT_DETAILS_MAX_LENGTH} characters). Otherwise tap Skip."
)
SKIP_BUTTON = "Skip"
DETAILS_TOO_LONG = f"Please keep it to {REPORT_DETAILS_MAX_LENGTH} characters, or tap Skip."
DETAILS_TEXT_ONLY = "Please send the details as text, or tap Skip."
DETAILS_ADDED = "Thanks, the details were added to your report."
DETAILS_SKIPPED = "OK, your report was sent without details."
DETAILS_DONE = "This report has already been sent."


def _button(text: str, data: SafetyAction) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def _confirm_keyboard(conversation_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(CONFIRM_YES, SafetyAction(action="confirm", conversation=conversation_id)),
                _button(CONFIRM_NO, SafetyAction(action="cancel", conversation=conversation_id)),
            ]
        ]
    )


def _reasons_keyboard(conversation_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    REASON_LABELS[reason],
                    SafetyAction(action="reason", conversation=conversation_id, reason=reason),
                )
            ]
            for reason in REPORT_REASONS
        ]
    )


def _skip_keyboard(conversation_id: int, report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    SKIP_BUTTON,
                    SafetyAction(action="skip", conversation=conversation_id, report=report_id),
                )
            ]
        ]
    )


async def _set_buttons(bot: Bot, query: CallbackQuery, markup: InlineKeyboardMarkup | None) -> None:
    """Replace the buttons on the message that was tapped. Best effort: Telegram may refuse,
    e.g. when they are unchanged."""
    if query.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=markup,
        )
    except TelegramAPIError as exc:
        logger.info("could not update buttons", extra=exception_summary(exc))


# --- Block -----------------------------------------------------------------------------


async def on_block(
    query: CallbackQuery, bot: Bot, callback_data: SafetyAction, session: AsyncSession
) -> None:
    conversation_id = callback_data.conversation
    user = await users.get_or_create_user(session, query.from_user.id)
    try:
        await moderation.partner_in(session, user.id, conversation_id)
    except NotPartnerError:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    active = await chat.active_chat(session, user.id)
    if active is not None and active.conversation_id == conversation_id:
        # Blocking ends this chat, so it is confirmed first.
        await session.commit()
        await query.answer()
        await bot.send_message(
            query.from_user.id, CONFIRM_BLOCK, reply_markup=_confirm_keyboard(conversation_id)
        )
        return
    # Not this chat any more: one tap blocks, and the button turns into Unblock.
    await _block(
        query, bot, session, user.id, conversation_id, safety_keyboard(conversation_id, True)
    )


async def on_confirm_block(
    query: CallbackQuery, bot: Bot, callback_data: SafetyAction, session: AsyncSession
) -> None:
    user = await users.get_or_create_user(session, query.from_user.id)
    await _block(query, bot, session, user.id, callback_data.conversation, None)


async def _block(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    user_id: int,
    conversation_id: int,
    buttons: InlineKeyboardMarkup | None,
) -> None:
    """Block the partner, then replace the tapped buttons with `buttons`.

    If that ended the chat, both users are told, as for any other ending. A repeated
    tap, or a chat that ended meanwhile, just blocks.
    """
    try:
        blocked = await moderation.block(session, user_id, conversation_id, datetime.now(UTC))
    except NotPartnerError:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    if not blocked.ended_chat:
        await session.commit()
        await query.answer(BLOCKED if blocked.added else ALREADY_BLOCKED)
        await _set_buttons(bot, query, buttons)
        return
    await tell_partner_ended(
        session, bot, user_id, chat.ActiveChat(conversation_id, blocked.partner_id)
    )
    await query.answer()
    await _set_buttons(bot, query, buttons)
    await send_chat_ended(bot, query.from_user.id, YOU_BLOCKED, conversation_id, blocked=True)


async def on_cancel_block(query: CallbackQuery, bot: Bot) -> None:
    await query.answer(NOT_BLOCKED)
    await _set_buttons(bot, query, None)


async def on_unblock(
    query: CallbackQuery, bot: Bot, callback_data: SafetyAction, session: AsyncSession
) -> None:
    conversation_id = callback_data.conversation
    user = await users.get_or_create_user(session, query.from_user.id)
    try:
        await moderation.unblock(session, user.id, conversation_id, datetime.now(UTC))
    except NotPartnerError:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    await session.commit()
    await query.answer(UNBLOCKED)
    await _set_buttons(bot, query, safety_keyboard(conversation_id, blocked=False))


# --- Report ----------------------------------------------------------------------------


async def on_report(
    query: CallbackQuery, bot: Bot, callback_data: SafetyAction, session: AsyncSession
) -> None:
    conversation_id = callback_data.conversation
    user = await users.get_or_create_user(session, query.from_user.id)
    try:
        await moderation.partner_in(session, user.id, conversation_id)
    except NotPartnerError:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    reported = await moderation.has_reported(session, user.id, conversation_id)
    await session.commit()
    if reported:
        await query.answer(ALREADY_REPORTED)
        return
    await query.answer()
    await bot.send_message(
        query.from_user.id, REPORT_PROMPT, reply_markup=_reasons_keyboard(conversation_id)
    )


async def on_reason(
    query: CallbackQuery,
    bot: Bot,
    callback_data: SafetyAction,
    session: AsyncSession,
    state: FSMContext,
    runtime_settings: RuntimeSettings,
    search_queue: SearchQueue,
) -> None:
    conversation_id = callback_data.conversation
    if callback_data.reason not in REPORT_REASONS:
        await query.answer(NOT_AVAILABLE)
        return
    user = await users.get_or_create_user(session, query.from_user.id)
    per_day = await runtime_settings.get(session, SettingKey.REPORTS_PER_DAY)
    try:
        filed = await moderation.file_report(
            session, user.id, conversation_id, callback_data.reason, datetime.now(UTC), per_day
        )
    except NotPartnerError:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    if filed.outcome is not ReportOutcome.FILED or filed.report_id is None:
        await session.commit()
        await query.answer(
            ALREADY_REPORTED if filed.outcome is ReportOutcome.ALREADY_REPORTED else LIMIT_REACHED,
            show_alert=filed.outcome is ReportOutcome.LIMIT_REACHED,
        )
        return
    if filed.ended_chat:
        await tell_partner_ended(
            session, bot, user.id, chat.ActiveChat(conversation_id, filed.partner_id)
        )
    in_chat = await chat.active_chat(session, user.id) is not None
    await session.commit()
    await query.answer()
    await _set_buttons(bot, query, None)
    if filed.ended_chat:
        await bot.send_message(query.from_user.id, REPORT_SENT_ENDED, reply_markup=main_keyboard())
    else:
        await bot.send_message(query.from_user.id, REPORT_SENT)
    if in_chat or await search_queue.current(user.id) is not None:
        # In a chat with someone else, or searching for one: the next message may belong
        # to that chat, so no details are asked for. A search started later clears
        # the details step, so it can't catch a new chat's messages either.
        return
    await state.set_state(ReportDetails.text)
    await state.set_data({"report": filed.report_id, "ended": filed.ended_chat})
    await bot.send_message(
        query.from_user.id,
        DETAILS_PROMPT,
        reply_markup=_skip_keyboard(conversation_id, filed.report_id),
    )


async def _finish_details(bot: Bot, chat_id: int, state: FSMContext, text: str) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("ended"):
        await send_menu(bot, chat_id, text)
    else:
        await bot.send_message(chat_id, text)


async def on_details(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    if message.from_user is None:
        return
    raw = message.text or ""
    if not raw.strip():
        await message.answer(DETAILS_TEXT_ONLY)
        return
    if len(raw.strip()) > REPORT_DETAILS_MAX_LENGTH:
        await message.answer(DETAILS_TOO_LONG)
        return
    report_id = (await state.get_data()).get("report")
    user = await users.get_or_create_user(session, message.from_user.id)
    added = isinstance(report_id, int) and await moderation.add_details(
        session, user.id, report_id, raw
    )
    await session.commit()
    await _finish_details(bot, message.chat.id, state, DETAILS_ADDED if added else DETAILS_DONE)


async def on_details_other(message: Message) -> None:
    await message.answer(DETAILS_TEXT_ONLY)


async def on_skip(
    query: CallbackQuery, bot: Bot, callback_data: SafetyAction, state: FSMContext
) -> None:
    data = await state.get_data()
    if await state.get_state() != ReportDetails.text or data.get("report") != callback_data.report:
        await query.answer(DETAILS_DONE)
        await _set_buttons(bot, query, None)
        return
    await query.answer()
    await _set_buttons(bot, query, None)
    await _finish_details(bot, query.from_user.id, state, DETAILS_SKIPPED)


def create_router() -> Router:
    router = Router(name="safety")
    # Private chats only: blocks and reports are never made from a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_block, SafetyAction.filter(F.action == "block"))
    router.callback_query.register(on_confirm_block, SafetyAction.filter(F.action == "confirm"))
    router.callback_query.register(on_cancel_block, SafetyAction.filter(F.action == "cancel"))
    router.callback_query.register(on_unblock, SafetyAction.filter(F.action == "unblock"))
    router.callback_query.register(on_report, SafetyAction.filter(F.action == "report"))
    router.callback_query.register(on_reason, SafetyAction.filter(F.action == "reason"))
    router.callback_query.register(on_skip, SafetyAction.filter(F.action == "skip"))
    router.message.register(on_details, ReportDetails.text, F.text, ~F.text.startswith("/"))
    router.message.register(on_details_other, ReportDetails.text, ~F.text.startswith("/"))
    return router
