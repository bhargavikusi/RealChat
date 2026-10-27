"""One database session per update, committed if the handler succeeds."""

from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.middlewares import Handler


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
