"""Daily reward reminders, as sent by the worker (D-060).

The reminder offers 🎁 Claim, the same button as in /coins, and 🔕 Turn off reminders,
which sets the ⏰ Reminders setting (D-052) as Advanced settings does.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from apps.bot.account import NotifyAction
from apps.bot.coins import CLAIM_BUTTON, DailyAction
from apps.bot.endings import coins_label
from apps.bot.relay import unreachable
from backend.core.logging import exception_summary
from backend.modules.users.service import Notification

logger = logging.getLogger(__name__)

REMINDER = "🎁 Your daily reward is waiting: {amount}."
TURN_OFF_BUTTON = "🔕 Turn off reminders"


def reminder_keyboard() -> InlineKeyboardMarkup:
    turn_off = NotifyAction(setting=Notification.REMINDERS.value, on=False)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CLAIM_BUTTON, callback_data=DailyAction(action="claim").pack()
                )
            ],
            [InlineKeyboardButton(text=TURN_OFF_BUTTON, callback_data=turn_off.pack())],
        ]
    )


class ReminderMessages:
    """Sends the reminder itself. Used by the worker."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def remind(self, telegram_id: int, amount: int) -> str:
        try:
            await self._bot.send_message(
                telegram_id,
                REMINDER.format(amount=coins_label(amount)),
                reply_markup=reminder_keyboard(),
            )
        except TelegramAPIError as exc:
            logger.info("could not send a daily reward reminder", extra=exception_summary(exc))
            return "unreachable" if unreachable(exc) else "failed"
        return "sent"
