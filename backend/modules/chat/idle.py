"""Ending quiet chats (D-039). Runs in the worker, once per tick.

A chat with no relayed message for the idle-notice time (admin setting, default 5
minutes) gets a warning to both users. If nobody writes within the grace time (default
60 seconds) after that, the chat ends with the reason "idle" and both are told. Any
relayed message clears the warning. Warnings and endings are committed before anyone
is told, and each happens once even with several workers.
"""

import logging
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat

logger = logging.getLogger(__name__)

BATCH_SIZE = 200


class IdleNotifier(Protocol):
    """Tells users about quiet chats. Implemented by the bot layer."""

    async def idle_warning(self, user_ids: tuple[int, int], quiet: int, grace: int) -> None: ...

    async def idle_ended(self, conversation_id: int, user_ids: tuple[int, int]) -> None: ...


class IdleChats:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
        notifier: IdleNotifier,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime_settings
        self._notifier = notifier

    async def tick(self, now: datetime) -> tuple[int, int]:
        """Warn and end quiet chats; returns (warned, ended)."""
        async with self._sessions() as session:
            quiet = await self._runtime.get(session, SettingKey.CHAT_IDLE_NOTICE_SECONDS)
            grace = await self._runtime.get(session, SettingKey.CHAT_IDLE_GRACE_SECONDS)
            # Ending first: a chat warned in this tick must get its full grace time.
            ended = await chat.end_idle_chats(session, now, timedelta(seconds=grace), BATCH_SIZE)
            warned = await chat.warn_idle_chats(session, now, timedelta(seconds=quiet), BATCH_SIZE)
            await session.commit()
        for idle in ended:
            await self._notify(self._notifier.idle_ended(idle.conversation_id, idle.user_ids))
        for idle in warned:
            await self._notify(self._notifier.idle_warning(idle.user_ids, quiet, grace))
        return len(warned), len(ended)

    @staticmethod
    async def _notify(call: Awaitable[None]) -> None:
        try:
            await call
        except Exception as exc:
            logger.warning("could not notify about an idle chat", extra=exception_summary(exc))
