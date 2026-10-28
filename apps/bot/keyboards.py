"""The keyboards under the message box (D-022, D-030).

The main keyboard holds the primary actions; while the user is in a chat it is
replaced by the chat keyboard. A button is added only once its feature works, so
Nearby, Browse, and Private mode join later.
"""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.chat import service as chat

NEW_CHAT = "🎭 New Anonymous Chat"
PARTNER_PROFILE = "👤 Partner profile"
END_CHAT = "⏹ End chat"


def _keyboard(*rows: list[str]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([NEW_CHAT])


def chat_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([PARTNER_PROFILE], [END_CHAT])


async def current_keyboard(session: AsyncSession, user_id: int) -> ReplyKeyboardMarkup:
    """The chat keyboard while the user is in a chat, otherwise the main keyboard.

    Reads the database; the caller commits before any Telegram call.
    """
    return chat_keyboard() if await chat.active_chat(session, user_id) else main_keyboard()
