"""Background worker: matching, idle chats, Private mode, and chat retention.

Run with: uv run python -m apps.worker.main

It uses the bot's configuration, because it sends Telegram messages (match
announcements, ended searches, and idle-chat warnings and endings). Only one matcher
runs at a time, even with several workers, thanks to a Redis lock; idle chats are
locked row by row in PostgreSQL, so each is warned and ended once (D-027, D-032,
D-039). Private mode sessions are ended when their chat ends or their time limit is
reached (D-047). Two jobs that call out or can run long have loops of their own, so they
never hold up matching: deleting Private mode messages from Telegram, and deleting
expired chat messages about once a day (D-046).
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime

from apps.bot.avatars import Avatars
from apps.bot.chat import ChatNotifier
from apps.bot.main import create_bot
from apps.bot.private import PrivateModeNotifier, TelegramDeletions
from backend.config.settings import load_bot_settings
from backend.core.logging import configure_logging, exception_summary
from backend.core.redis import create_redis
from backend.database.engine import create_engine, create_session_factory
from backend.modules.admin.settings import RuntimeSettings
from backend.modules.chat.idle import IdleChats
from backend.modules.chat.private import PrivateModeJob
from backend.modules.chat.retention import ChatRetention
from backend.modules.matching.matcher import Matcher
from backend.modules.users.terms import TERMS_VERSION

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0
# The retention job does its work once a day; this is only how often it checks.
RETENTION_CHECK_SECONDS = 60.0
DELETIONS_SECONDS = 1.0

Job = Callable[[datetime], Awaitable[object]]


async def _repeat(jobs: tuple[tuple[str, Job], ...], every: float, stop: asyncio.Event) -> None:
    """Run `jobs` in turn every `every` seconds until `stop` is set. A failed job is
    logged and the next round runs as usual."""
    while not stop.is_set():
        for name, job in jobs:
            try:
                await job(datetime.now(UTC))
            except Exception as exc:
                logger.error(f"{name} tick failed", extra=exception_summary(exc))
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def run_jobs(
    main: tuple[tuple[str, Job], ...],
    separate: tuple[tuple[str, Job, float], ...],
    stop: asyncio.Event,
) -> None:
    """Run `main` in turn every tick, and each of `separate` in a loop of its own at its
    own interval, until `stop` is set. A slow separate job never holds up the main
    loop (they share only the database)."""
    loops = [
        asyncio.create_task(_repeat(((name, job),), every, stop)) for name, job, every in separate
    ]
    try:
        await _repeat(main, TICK_SECONDS, stop)
    finally:
        # Each of their batches commits on its own, so stopping midway loses nothing.
        for task in loops:
            task.cancel()
        for task in loops:
            with suppress(asyncio.CancelledError):
                await task


async def run() -> None:
    settings = load_bot_settings()
    configure_logging(settings.log_level)
    redis = create_redis(settings.redis_url.get_secret_value())
    engine = create_engine(settings.database_url.get_secret_value())
    sessions = create_session_factory(engine)
    bot = create_bot(settings)
    runtime_settings = RuntimeSettings()
    notifier = ChatNotifier(bot, sessions, Avatars(redis))
    matcher = Matcher(redis, sessions, runtime_settings, notifier, TERMS_VERSION)
    idle_chats = IdleChats(sessions, runtime_settings, notifier)
    retention = ChatRetention(redis, sessions, runtime_settings)
    private_mode = PrivateModeJob(sessions, runtime_settings, PrivateModeNotifier(bot, sessions))
    deletions = TelegramDeletions(bot, sessions)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        # Not available on every platform (e.g. Windows); asyncio.run still stops on
        # Ctrl+C there, just without the clean shutdown message.
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    try:
        logger.info("worker starting")
        await run_jobs(
            (
                ("matcher", matcher.tick),
                ("idle chats", idle_chats.tick),
                ("private mode", private_mode.tick),
            ),
            (
                ("private mode deletions", deletions.tick, DELETIONS_SECONDS),
                ("chat retention", retention.tick, RETENTION_CHECK_SECONDS),
            ),
            stop,
        )
        logger.info("worker stopped")
    finally:
        await bot.session.close()
        await engine.dispose()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
