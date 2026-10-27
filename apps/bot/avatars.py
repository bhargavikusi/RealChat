"""Default avatars (D-020): one neutral image per gender for profiles without a photo.

Each image is uploaded to Telegram the first time it is needed and then reused by the
file identifier Telegram returns. File identifiers are specific to a bot, so they are
cached in Redis per bot. If Redis is unavailable, or a cached identifier is rejected,
the image is simply uploaded again.
"""

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.modules.users.validation import Gender

logger = logging.getLogger(__name__)

AVATAR_DIR = Path(__file__).parent / "assets" / "avatars"


def avatar_path(gender: Gender) -> Path:
    return AVATAR_DIR / f"{gender.value}.png"


class Avatars:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def send(
        self,
        bot: Bot,
        chat_id: int,
        gender: Gender,
        caption: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        key = f"avatar_file_id:{bot.id}:{gender.value}"
        cached = await self._get(key)
        if cached is not None:
            try:
                await bot.send_photo(chat_id, cached, caption=caption, reply_markup=reply_markup)
                return
            except TelegramBadRequest:
                logger.warning(
                    "cached avatar rejected, uploading again", extra={"gender": gender.value}
                )
                await self._delete(key)
        sent = await bot.send_photo(
            chat_id, FSInputFile(avatar_path(gender)), caption=caption, reply_markup=reply_markup
        )
        if sent.photo:
            await self._set(key, sent.photo[-1].file_id)

    # Redis failures only cost an extra upload, so they are logged and ignored.

    async def _get(self, key: str) -> str | None:
        try:
            value = await self._redis.get(key)
        except RedisError:
            logger.warning("avatar cache unavailable")
            return None
        return None if value is None else str(value)

    async def _set(self, key: str, file_id: str) -> None:
        try:
            await self._redis.set(key, file_id)
        except RedisError:
            logger.warning("avatar cache unavailable")

    async def _delete(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except RedisError:
            logger.warning("avatar cache unavailable")
