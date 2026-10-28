"""One simulated user talking to the real dispatcher, for end-to-end bot tests.

Updates go through the full middleware and router stack against real PostgreSQL
and Redis; only Telegram itself is simulated (see `tests.support.telegram`).
"""

from itertools import count

from aiogram import Dispatcher
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import onboarding as ob
from apps.bot.main import create_dispatcher
from backend.config.settings import BotSettings
from backend.modules.users import service
from backend.modules.users.models import Profile
from backend.modules.users.terms import terms_text
from tests.support.telegram import (
    RecordingSession,
    callback_update,
    make_bot,
    message_update,
    photo_update,
)

Sessions = async_sessionmaker[AsyncSession]
USER_ID = 4242
TERMS = terms_text(18)


class Chat:
    """One user talking to the bot. `restart()` simulates a bot process restart."""

    _ids = count(1)

    def __init__(
        self, settings: BotSettings, redis: Redis, sessions: Sessions, user_id: int = USER_ID
    ) -> None:
        self._settings, self._redis, self._sessions = settings, redis, sessions
        self.user_id = user_id
        self.bot, self.telegram = make_bot()
        self.dispatcher: Dispatcher = create_dispatcher(settings, redis, sessions)

    def restart(self) -> None:
        self.bot, self.telegram = make_bot()
        self.dispatcher = create_dispatcher(self._settings, self._redis, self._sessions)

    async def send(self, text: str) -> str:
        await self.dispatcher.feed_update(
            self.bot, message_update(next(self._ids), self.user_id, text)
        )
        return self.last()

    async def press(self, button: str) -> str:
        data = self.telegram.last_buttons()[button]
        await self.dispatcher.feed_update(
            self.bot, callback_update(next(self._ids), self.user_id, data)
        )
        return self.last()

    async def press_data(self, data: str) -> None:
        await self.dispatcher.feed_update(
            self.bot, callback_update(next(self._ids), self.user_id, data)
        )

    async def send_photo(self, file_id: str) -> str:
        await self.dispatcher.feed_update(
            self.bot, photo_update(next(self._ids), self.user_id, file_id)
        )
        return self.last()

    def last(self) -> str:
        texts = self.telegram.sent_texts()
        return texts[-1] if texts else ""

    @property
    def session(self) -> RecordingSession:
        return self.telegram


async def load_profile(sessions: Sessions) -> Profile:
    async with sessions() as session:
        user = await service.get_or_create_user(session, USER_ID)
        return await service.get_profile(session, user.id)


async def complete_required_steps(chat: Chat) -> None:
    assert await chat.send("/start") == TERMS
    assert await chat.press("I agree") == ob.PROMPTS["nickname"]
    assert await chat.send("Night Owl") == ob.PROMPTS["age"]
    assert await chat.send("24") == ob.PROMPTS["gender"]
    assert await chat.press("Female") == ob.PROMPTS["country"]
    assert await chat.send("india") == ob.COUNTRY_PICK
    assert await chat.press("India") == ob.PROMPTS["city"]


async def complete_onboarding(chat: Chat) -> None:
    """Required steps, then skip city and photo: a fully onboarded user."""
    await complete_required_steps(chat)
    assert await chat.press("Skip") == ob.PROMPTS["photo"]
    assert await chat.press("Skip") == ob.ONBOARDING_COMPLETE
