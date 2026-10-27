"""Telegram bot entry point (long polling).

Run with: uv run python -m apps.bot.main
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from redis.asyncio import Redis

from apps.bot import errors, start
from apps.bot.middlewares import DeduplicateUpdatesMiddleware, FloodControlMiddleware
from backend.config.settings import BotSettings, load_bot_settings
from backend.core.logging import configure_logging
from backend.core.redis import create_redis

logger = logging.getLogger(__name__)


def create_dispatcher(settings: BotSettings, redis: Redis) -> Dispatcher:
    dispatcher = Dispatcher()
    # Order matters: duplicates are discarded before they count toward flood limits.
    dispatcher.update.outer_middleware(
        DeduplicateUpdatesMiddleware(redis, ttl_seconds=settings.bot_update_dedup_ttl_seconds)
    )
    dispatcher.update.outer_middleware(
        FloodControlMiddleware(
            redis,
            limit=settings.bot_flood_limit,
            window_seconds=settings.bot_flood_window_seconds,
        )
    )
    dispatcher.include_routers(errors.create_router(), start.create_router())
    return dispatcher


def create_bot(settings: BotSettings) -> Bot:
    # No default parse mode: text is sent as plain text, so user-provided content
    # can never be interpreted as HTML or Markdown.
    return Bot(token=settings.telegram_bot_token.get_secret_value())


async def run() -> None:
    settings = load_bot_settings()
    configure_logging(settings.log_level)
    redis = create_redis(settings.redis_url.get_secret_value())
    bot = create_bot(settings)
    dispatcher = create_dispatcher(settings, redis)
    try:
        logger.info("bot starting (long polling)")
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types(), handle_signals=True
        )
    finally:
        await bot.session.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
