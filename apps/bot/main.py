"""Telegram bot entry point (long polling).

Run with: uv run python -m apps.bot.main
"""

import asyncio
import logging
import time
from collections.abc import Callable

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.types import BotCommandScopeAllPrivateChats
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account, admin, errors, menu, onboarding, profile
from apps.bot.account import AccessGateMiddleware
from apps.bot.avatars import Avatars
from apps.bot.db import DbSessionMiddleware
from apps.bot.middlewares import DeduplicateUpdatesMiddleware, FloodControlMiddleware
from apps.bot.telegram_session import install_request_middlewares
from backend.config.settings import BotSettings, load_bot_settings
from backend.core.logging import configure_logging
from backend.core.redis import create_redis
from backend.database.engine import create_engine, create_session_factory
from backend.modules.admin.settings import RuntimeSettings

logger = logging.getLogger(__name__)


def create_dispatcher(
    settings: BotSettings,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Callable[[], float] = time.time,
) -> Dispatcher:
    # FSM state (position within optional onboarding steps) lives in Redis so it
    # survives restarts; durable progress is in PostgreSQL. `settings` is passed to
    # every handler that declares a `settings` parameter, and `avatars` and
    # `runtime_settings` (admin-changeable values, D-035) likewise.
    # Event isolation processes one update at a time per user and chat, so a quick
    # second message cannot be routed by the FSM state the first one is changing.
    # The locks live in Redis: they expire on their own (no per-user memory kept in
    # the process) and remain correct if several webhook workers are used (O-006).
    dispatcher = Dispatcher(
        storage=RedisStorage(redis),
        events_isolation=RedisEventIsolation(redis),
        settings=settings,
        avatars=Avatars(redis),
        runtime_settings=RuntimeSettings(),
    )
    # Order matters: duplicates are discarded before they count toward flood limits.
    dispatcher.update.outer_middleware(
        DeduplicateUpdatesMiddleware(
            redis,
            ttl_seconds=settings.bot_update_dedup_ttl_seconds,
            lease_seconds=settings.bot_update_lease_seconds,
        )
    )
    dispatcher.update.outer_middleware(
        FloodControlMiddleware(
            redis,
            limit=settings.bot_flood_limit,
            window_seconds=settings.bot_flood_window_seconds,
            clock=clock,
        )
    )
    # Inner middleware: runs only for updates that passed deduplication and flood control.
    dispatcher.update.middleware(DbSessionMiddleware(session_factory))
    # Then suspended and banned users are stopped before any feature handler (D-026).
    dispatcher.update.middleware(AccessGateMiddleware())
    # Onboarding comes last: its final handler answers any message no other router took.
    dispatcher.include_routers(
        errors.create_router(),
        menu.create_router(),
        account.create_router(),
        admin.create_router(),
        profile.create_router(),
        onboarding.create_router(),
    )
    return dispatcher


def create_bot(settings: BotSettings) -> Bot:
    # No default parse mode: text is sent as plain text, so user-provided content
    # can never be interpreted as HTML or Markdown.
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    install_request_middlewares(bot.session)
    return bot


async def run() -> None:
    settings = load_bot_settings()
    configure_logging(settings.log_level)
    redis = create_redis(settings.redis_url.get_secret_value())
    engine = create_engine(settings.database_url.get_secret_value())
    bot = create_bot(settings)
    dispatcher = create_dispatcher(settings, redis, create_session_factory(engine))
    try:
        # The menu next to the message box (D-022); private chats only.
        await bot.set_my_commands(menu.BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        logger.info("bot starting (long polling)")
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types(), handle_signals=True
        )
    finally:
        await bot.session.close()
        await engine.dispose()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
