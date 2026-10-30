"""The keyboards under the message box (D-022, D-030).

The main keyboard holds the primary actions; while the user is in a chat it is
replaced by the chat keyboard, whose Private mode button shows whether it is on
(D-047). A button is added only once its feature works, so Nearby and Browse join later.
"""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.chat import private
from backend.modules.chat import service as chat

NEW_CHAT = "🎭 New Anonymous Chat"
PARTNER_PROFILE = "👤 Partner profile"
END_CHAT = "⏹ End chat"
PRIVATE_ON = "🔒 Private mode"
PRIVATE_OFF = "🔓 Private mode off"


def _keyboard(*rows: list[str]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([NEW_CHAT])


def chat_keyboard(private: bool = False) -> ReplyKeyboardMarkup:
    """The chat controls (D-030); `private`: whether Private mode is on in the chat."""
    return _keyboard([PARTNER_PROFILE, PRIVATE_OFF if private else PRIVATE_ON], [END_CHAT])


async def current_keyboard(session: AsyncSession, user_id: int) -> ReplyKeyboardMarkup:
    """The chat keyboard while the user is in a chat, otherwise the main keyboard.

    Reads the database; the caller commits before any Telegram call.
    """
    active = await chat.active_chat(session, user_id)
    if active is None:
        return main_keyboard()
    return chat_keyboard(await private.active_session(session, active.conversation_id) is not None)
