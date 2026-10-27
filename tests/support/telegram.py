"""A simulated Telegram for tests: no network, no real bot.

`RecordingSession` replaces the HTTP session, so every Bot API call is captured
instead of sent. Helper functions build the incoming updates a real user would send.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User

# Obviously fake, see tests/unit/test_logging.py for why it avoids the real token prefix.
FAKE_TOKEN = "123456789:TEST-fake-token-for-bot-tests-only"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 (aiogram interface)
    ) -> TelegramType:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            sent = Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
            return sent  # type: ignore[return-value]
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
        return [m.text for m in self.requests if isinstance(m, SendMessage)]


def make_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession()
    return Bot(token=FAKE_TOKEN, session=session), session


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def message_update(update_id: int, user_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=_user(user_id),
            text=text,
        ),
    )


def callback_update(update_id: int, user_id: int, data: str) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id), from_user=_user(user_id), chat_instance="test", data=data
        ),
    )
