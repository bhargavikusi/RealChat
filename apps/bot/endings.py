"""The new-chat menu, and telling users how a chat ended (D-044).

🎭 New Anonymous Chat opens a "Who do you want to meet?" message with one button per
kind of chat. Only the free random chat exists so far; paid choices join it with the
coin work. Whenever a chat ends, however it ends, each user is told how, the main
keyboard comes back, and the same menu is shown, so the next chat is one tap away.

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


async def send_chat_ended(bot: Bot, chat_id: int, how: str) -> None:
    """Say how the chat ended, bring back the main keyboard, then offer a new chat.

    Two messages, because a Telegram message carries either the keyboard under the
    message box or buttons of its own, not both.
    """
    await bot.send_message(chat_id, how, reply_markup=main_keyboard())
    await send_menu(bot, chat_id)


async def tell_chat_ended(bot: Bot, chat_id: int, how: str) -> None:
    """`send_chat_ended` for the other user, who may have blocked the bot: never raises."""
    try:
        await send_chat_ended(bot, chat_id, how)
    except TelegramAPIError as exc:
        logger.warning("could not tell a user the chat ended", extra=exception_summary(exc))
