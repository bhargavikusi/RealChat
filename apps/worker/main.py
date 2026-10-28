"""Background worker: runs the matcher about once a second (D-027, D-032).

Run with: uv run python -m apps.worker.main

It uses the bot's configuration, because it sends Telegram messages (match
announcements and ended searches). Only one matcher runs at a time, even with
several workers, thanks to a Redis lock; an extra worker just waits its turn.
"""

import asyncio
import logging
import signal
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
from backend.modules.matching.matcher import Matcher
from backend.modules.users.terms import TERMS_VERSION

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0


async def run_matcher(matcher: Matcher, stop: asyncio.Event) -> None:
    """Tick until `stop` is set. A failed tick is logged and the next one runs as usual."""
    while not stop.is_set():
        try:
            await matcher.tick(datetime.now(UTC))
        except Exception as exc:
            logger.error("matcher tick failed", extra=exception_summary(exc))
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)


async def run() -> None:
    settings = load_bot_settings()
    configure_logging(settings.log_level)
    redis = create_redis(settings.redis_url.get_secret_value())
    engine = create_engine(settings.database_url.get_secret_value())
    sessions = create_session_factory(engine)
    bot = create_bot(settings)
    matcher = Matcher(
        redis,
        sessions,
        RuntimeSettings(),
        ChatNotifier(bot, sessions, Avatars(redis)),
        TERMS_VERSION,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        # Not available on every platform (e.g. Windows); asyncio.run still stops on
        # Ctrl+C there, just without the clean shutdown message.
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    try:
        logger.info("worker starting")
        await run_matcher(matcher, stop)
        logger.info("worker stopped")
    finally:
        await bot.session.close()
        await engine.dispose()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
