"""The messages sent when a chat ends (D-044, D-045)."""

from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from apps.bot.endings import MEET_PROMPT, SAFETY_PROMPT, YOU_ENDED, send_chat_ended


class FailingSafetyPrompt:
    """A bot whose "Anything wrong with this chat?" message fails to send."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        if text == SAFETY_PROMPT:
            method = SendMessage(chat_id=chat_id, text=text)
            raise TelegramBadRequest(method=method, message="Bad Request: try again")
        self.sent.append(text)


async def test_the_menu_follows_even_if_block_and_report_cannot_be_offered() -> None:
    bot = FailingSafetyPrompt()
    await send_chat_ended(cast(Bot, bot), 1, YOU_ENDED, 7, blocked=False)
    assert bot.sent == [YOU_ENDED, MEET_PROMPT]
