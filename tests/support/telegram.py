"""A simulated Telegram for tests: no network, no real bot.

`RecordingSession` replaces the HTTP session, so every Bot API call is captured
instead of sent. Helper functions build the incoming updates a real user would send.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage, SendPhoto, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    PhotoSize,
    Update,
    User,
)

from apps.bot.telegram_session import install_request_middlewares

# Obviously fake, see tests/unit/test_logging.py for why it avoids the real token prefix.
FAKE_TOKEN = "123456789:TEST-fake-token-for-bot-tests-only"
BOT_USER = User(id=123456789, is_bot=True, first_name="RealChat Test", username="RealChatTestBot")


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        # When set, answerCallbackQuery fails with this Telegram error description.
        self.reject_callback_answers: str | None = None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 (aiogram interface)
    ) -> TelegramType:
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery) and self.reject_callback_answers:
            raise TelegramBadRequest(method=method, message=self.reject_callback_answers)
        if isinstance(method, SendMessage):
            sent = Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
            return sent  # type: ignore[return-value]
        if isinstance(method, SendPhoto):
            # An uploaded file gets a new identifier, as Telegram assigns one; a file
            # identifier that was sent is echoed back unchanged.
            file_id = (
                f"uploaded-{len(self.requests)}"
                if isinstance(method.photo, InputFile)
                else str(method.photo)
            )
            sent = Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                caption=method.caption,
                # Distinct from file_id, as on Telegram, so caching the wrong one shows up.
                photo=[
                    PhotoSize(
                        file_id=file_id,
                        file_unique_id=f"unique-{len(self.requests)}",
                        width=512,
                        height=512,
                    )
                ],
            )
            return sent  # type: ignore[return-value]
        if isinstance(method, GetMe):
            return BOT_USER  # type: ignore[return-value]
        # Most other methods (answerCallbackQuery, etc.) return True.
        return True  # type: ignore[return-value]

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 (aiogram interface)
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise NotImplementedError("file downloads are not simulated")
        yield b""  # pragma: no cover

    async def close(self) -> None:
        pass

    def sent_texts(self) -> list[str]:
        """Text of every message sent, using the caption for photos."""
        return [
            m.text if isinstance(m, SendMessage) else m.caption or ""
            for m in self.requests
            if isinstance(m, SendMessage | SendPhoto)
        ]

    def sent_to(self, chat_id: int) -> list[SendMessage | SendPhoto]:
        """Messages and photos sent to one chat, oldest first."""
        return [
            m
            for m in self.requests
            if isinstance(m, SendMessage | SendPhoto) and int(m.chat_id) == chat_id
        ]

    def callback_answers(self) -> list[str]:
        """Text of every callback answer (the short notice shown on a button press)."""
        return [m.text or "" for m in self.requests if isinstance(m, AnswerCallbackQuery)]

    def sent_photos(self) -> list[SendPhoto]:
        return [m for m in self.requests if isinstance(m, SendPhoto)]

    def last_buttons(self) -> dict[str, str]:
        """Button text -> callback data (or URL) for the latest message that had buttons."""
        for method in reversed(self.requests):
            if isinstance(method, SendMessage | SendPhoto) and isinstance(
                method.reply_markup, InlineKeyboardMarkup
            ):
                return {
                    button.text: str(button.callback_data or button.url)
                    for row in method.reply_markup.inline_keyboard
                    for button in row
                }
        return {}


def make_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession()
    # The same request middlewares as the real bot (apps.bot.main.create_bot).
    install_request_middlewares(session)
    return Bot(token=FAKE_TOKEN, session=session), session


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _chat(user_id: int, chat_type: str) -> Chat:
    # A private chat's id equals the user's id; groups get a negative id like Telegram's.
    return Chat(id=user_id if chat_type == "private" else -user_id, type=chat_type)


def message_update(update_id: int, user_id: int, text: str, chat_type: str = "private") -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=_chat(user_id, chat_type),
            from_user=_user(user_id),
            text=text,
        ),
    )


def callback_update(update_id: int, user_id: int, data: str, chat_type: str = "private") -> Update:
    # The message the button was attached to, as Telegram includes it.
    button_message = Message(
        message_id=update_id, date=datetime.now(UTC), chat=_chat(user_id, chat_type), text="-"
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=_user(user_id),
            chat_instance="test",
            data=data,
            message=button_message,
        ),
    )


def photo_update(update_id: int, user_id: int, file_id: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=_user(user_id),
            photo=[
                PhotoSize(file_id=f"{file_id}-small", file_unique_id="s", width=90, height=90),
                PhotoSize(file_id=file_id, file_unique_id="l", width=800, height=800),
            ],
        ),
    )
