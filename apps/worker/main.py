"""Background worker: matching, idle chats, and chat retention (D-027, D-032, D-039, D-046).

Run with: uv run python -m apps.worker.main

It uses the bot's configuration, because it sends Telegram messages (match
announcements, ended searches, and idle-chat warnings and endings). Only one matcher
runs at a time, even with several workers, thanks to a Redis lock; idle chats are
locked row by row in PostgreSQL, so each is warned and ended once. Expired chat
messages are deleted once a day (checked every tick, run when due).
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
from backend.config.settings import load_bot_settings
from backend.core.logging import configure_logging, exception_summary
from backend.core.redis import create_redis
from backend.database.engine import create_engine, create_session_factory
from backend.modules.admin.settings import RuntimeSettings
from backend.modules.chat.idle import IdleChats
from backend.modules.chat.retention import ChatRetention
from backend.modules.matching.matcher import Matcher
from backend.modules.users.terms import TERMS_VERSION

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0


async def run_jobs(
    matcher: Matcher, idle_chats: IdleChats, retention: ChatRetention, stop: asyncio.Event
) -> None:
    """Tick until `stop` is set. A failed job is logged and the next tick runs as usual."""
    jobs: tuple[tuple[str, Callable[[datetime], Awaitable[object]]], ...] = (
        ("matcher", matcher.tick),
        ("idle chats", idle_chats.tick),
        ("chat retention", retention.tick),
    )
    while not stop.is_set():
        for name, job in jobs:
            try:
                await job(datetime.now(UTC))
            except Exception as exc:
                logger.error(f"{name} tick failed", extra=exception_summary(exc))
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)


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
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        # Not available on every platform (e.g. Windows); asyncio.run still stops on
        # Ctrl+C there, just without the clean shutdown message.
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    try:
        logger.info("worker starting")
        await run_jobs(matcher, idle_chats, retention, stop)
        logger.info("worker stopped")
    finally:
        await bot.session.close()
        await engine.dispose()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
