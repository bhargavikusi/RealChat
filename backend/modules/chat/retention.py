"""Deleting stored chat messages after the retention period (D-028, D-046).

Runs in the worker. A stored message older than the retention period (admin setting,
default 7 days) is deleted entirely: its text, edits, file identifier, and the record
of who sent it. Messages in a conversation with an open report are kept until the
report is closed. Conversations, participants, blocks, reports, and audit records are
never touched.

The job runs at most once a day across all workers (a Redis key marks the last run;
after a failure it is tried again an hour later). It deletes in bounded batches, each
in its own short transaction, and logs only counts. Running it again deletes nothing
more than what has expired since. Deleting a stored message does not remove it from
either user's Telegram chat.
"""

import logging
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat.models import ChatMessage
from backend.modules.moderation.models import Report

logger = logging.getLogger(__name__)

RUN_KEY = "retention:chat_messages:last_run"
RUN_INTERVAL = timedelta(days=1)
# After a failed run, the next attempt waits this long instead of a full day.
RETRY_INTERVAL = timedelta(hours=1)
BATCH_SIZE = 1_000


async def delete_expired_messages(session: AsyncSession, cutoff: datetime, limit: int) -> int:
    """Delete up to `limit` messages created before `cutoff`, except those in a
    conversation with an open report; returns how many were deleted."""
    under_report = exists().where(
        Report.conversation_id == ChatMessage.conversation_id, Report.status == "open"
    )
    expired = (
        select(ChatMessage.id)
        .where(ChatMessage.created_at < cutoff, ~under_report)
        .order_by(ChatMessage.created_at)
        .limit(limit)
        # Rows being written by the relay are left for the next run.
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        delete(ChatMessage)
        .where(ChatMessage.id.in_(expired.scalar_subquery()))
        .returning(ChatMessage.id)
    )
    return len(result.all())


class ChatRetention:
    def __init__(
        self,
        redis: Redis,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
    ) -> None:
        self._redis = redis
        self._sessions = sessions
        self._runtime = runtime_settings

    async def tick(self, now: datetime) -> int | None:
        """Run the cleanup if it hasn't run in the last day; returns the number deleted,
        or None if it was not due."""
        due = await self._redis.set(
            RUN_KEY, now.isoformat(), nx=True, ex=int(RUN_INTERVAL.total_seconds())
        )
        if not due:
            return None
        try:
            return await self.run(now)
        except BaseException:
            await self._redis.set(RUN_KEY, now.isoformat(), ex=int(RETRY_INTERVAL.total_seconds()))
            raise

    async def run(self, now: datetime) -> int:
        """Delete every expired message, batch by batch; returns how many were deleted."""
        async with self._sessions() as session:
            days = await self._runtime.get(session, SettingKey.CHAT_RETENTION_DAYS)
            await session.commit()
        cutoff = now - timedelta(days=days)
        total = 0
        while True:
            async with self._sessions() as session:
                deleted = await delete_expired_messages(session, cutoff, BATCH_SIZE)
                await session.commit()
            total += deleted
            if deleted < BATCH_SIZE:
                break
        logger.info("deleted expired chat messages", extra={"count": total})
        return total
