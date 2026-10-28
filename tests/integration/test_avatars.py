"""When a cached avatar file identifier is retried (D-020)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from aiogram.types import Chat, Message, PhotoSize
from redis.asyncio import Redis

from apps.bot.avatars import Avatars
from backend.modules.users.validation import Gender

KEY = "avatar_file_id:7:other"


class StubBot:
    """Fails sends of the cached identifier with `error`; uploads succeed."""

    id = 7

    def __init__(self, error: str) -> None:
        self.error = error
        self.uploads = 0

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> Message:
        if isinstance(photo, str):
            raise TelegramBadRequest(
                method=SendPhoto(chat_id=chat_id, photo=photo), message=self.error
            )
        self.uploads += 1
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            photo=[PhotoSize(file_id="new-id", file_unique_id="u", width=1, height=1)],
        )


async def test_a_rejected_file_identifier_is_uploaded_again(redis: Redis) -> None:
    await redis.set(KEY, "stale-id")
    bot = StubBot("Bad Request: wrong file identifier/HTTP URL specified")
    await Avatars(redis).send(bot, 42, Gender.OTHER, "caption")  # type: ignore[arg-type]
    assert bot.uploads == 1
    assert await redis.get(KEY) == "new-id"


async def test_other_errors_are_raised_and_keep_the_cache(redis: Redis) -> None:
    await redis.set(KEY, "good-id")
    bot = StubBot("Bad Request: chat not found")
    with pytest.raises(TelegramBadRequest):
        await Avatars(redis).send(bot, 42, Gender.OTHER, "caption")  # type: ignore[arg-type]
    assert bot.uploads == 0
    assert await redis.get(KEY) == "good-id"
