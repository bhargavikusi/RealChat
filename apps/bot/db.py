"""One database session per update, committed if the handler succeeds, and the
RealChat activity recorded for each update (D-051)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.middlewares import Handler
from backend.modules.users import service as users


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            # Leaving the context without commit rolls back, including on exceptions.
            result = await handler(event, data)
            await session.commit()
            return result


# How often a busy user's RealChat activity is written (D-051).
ACTIVITY_INTERVAL = timedelta(seconds=60)


class ActivityMiddleware(BaseMiddleware):
    """Records RealChat activity for every private-chat update that reaches the handlers.

    Registered after the access gate, so suspended and banned users are not counted, and
    committed before the handler runs, so the write holds no lock while it works.
    """

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        session: AsyncSession | None = data.get("session")
        if (
            user is not None
            and session is not None
            and chat is not None
            and chat.type == ChatType.PRIVATE
        ):
            await users.record_activity(session, user.id, self._clock(), ACTIVITY_INTERVAL)
            await session.commit()
        return await handler(event, data)
