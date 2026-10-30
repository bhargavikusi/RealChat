"""The new-chat menu, and telling users how a chat ended (D-044).

🎭 New Anonymous Chat opens a "Who do you want to meet?" message with one button per
kind of chat. Only the free random chat exists so far; paid choices join it with the
coin work. Whenever a chat ends, however it ends, each user is told how, the main
keyboard comes back, Block and Report are offered for that chat (D-045), and the same
menu is shown, so the next chat is one tap away.

This module depends only on the keyboards, so the chat, relay, account, and worker
code can all use it.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from apps.bot.keyboards import main_keyboard
from backend.core.logging import exception_summary

logger = logging.getLogger(__name__)


class SearchAction(CallbackData, prefix="search"):
    action: str  # "start" (a random chat) or "cancel"
    search: str = ""  # the search a "cancel" button belongs to


class SafetyAction(CallbackData, prefix="safety"):
    # "block", "confirm" or "cancel" (a block that ends the chat), "unblock", "report",
    # "reason" (a report reason was chosen), or "skip" (no report details)
    action: str
    conversation: int  # the chat whose partner is blocked or reported
    reason: str = ""
    report: int = 0  # the report a "skip" button belongs to


MEET_PROMPT = "Who do you want to meet?"
RANDOM_BUTTON = "🎲 Random chat · Free"

YOU_ENDED = "You ended the chat."
PARTNER_ENDED = "Your chat partner ended the chat."
PARTNER_LEFT = "Your chat partner left RealChat, so the chat has ended."
PARTNER_UNREACHABLE = (
    "Your chat partner can't be reached any more, so the chat has ended. "
    "Your last message wasn't delivered."
)
IDLE_ENDED = "The chat ended because nobody wrote for a while."

SAFETY_PROMPT = "Anything wrong with this chat?"
BLOCK_BUTTON = "🚫 Block"
UNBLOCK_BUTTON = "✅ Unblock"
REPORT_BUTTON = "⚠️ Report"


def safety_keyboard(conversation_id: int, blocked: bool) -> InlineKeyboardMarkup:
    """Block (or Unblock, once blocked) and Report for the partner in a chat (D-045)."""
    toggle = (
        (UNBLOCK_BUTTON, SafetyAction(action="unblock", conversation=conversation_id))
        if blocked
        else (BLOCK_BUTTON, SafetyAction(action="block", conversation=conversation_id))
    )
    report = (REPORT_BUTTON, SafetyAction(action="report", conversation=conversation_id))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, callback_data=data.pack())
                for text, data in (toggle, report)
            ]
        ]
    )


def new_chat_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=RANDOM_BUTTON, callback_data=SearchAction(action="start").pack()
                )
            ]
        ]
    )


async def send_menu(bot: Bot, chat_id: int, intro: str | None = None) -> None:
    """The "Who do you want to meet?" message, optionally after a line of context."""
    text = MEET_PROMPT if intro is None else f"{intro}\n\n{MEET_PROMPT}"
    await bot.send_message(chat_id, text, reply_markup=new_chat_menu())


async def send_chat_ended(
    bot: Bot, chat_id: int, how: str, conversation_id: int, *, blocked: bool
) -> None:
    """Say how the chat ended and bring back the main keyboard, then offer Block and
    Report for the partner (`blocked`: whether this user has blocked them) and a new chat.

    Separate messages, because a Telegram message carries either the keyboard under the
    message box or buttons of its own, not both. The layout is provisional (D-045).
    """
    await bot.send_message(chat_id, how, reply_markup=main_keyboard())
    await bot.send_message(
        chat_id, SAFETY_PROMPT, reply_markup=safety_keyboard(conversation_id, blocked)
    )
    await send_menu(bot, chat_id)


async def tell_chat_ended(
    bot: Bot, chat_id: int, how: str, conversation_id: int, *, blocked: bool
) -> None:
    """`send_chat_ended` for the other user, who may have blocked the bot: never raises."""
    try:
        await send_chat_ended(bot, chat_id, how, conversation_id, blocked=blocked)
    except TelegramAPIError as exc:
        logger.warning("could not tell a user the chat ended", extra=exception_summary(exc))
