"""Database helpers for concurrency tests."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def wait_until_blocked(
    sessions: async_sessionmaker[AsyncSession], within_seconds: float = 5
) -> None:
    """Wait until another connection to this database is waiting on a lock.

    Concurrency tests use it to be sure the second transaction really reached the
    lock held by the first, instead of sleeping and hoping it got there.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within_seconds
    async with sessions() as session:
        while True:
            blocked = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            await session.rollback()
            if blocked:
                return
            if loop.time() > deadline:
                raise AssertionError("no transaction reached the lock in time")
            await asyncio.sleep(0.01)
