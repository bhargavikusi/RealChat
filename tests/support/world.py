"""Alice and Bob on the bot plus the worker's matcher, for two-user chat tests."""

from datetime import UTC, datetime

from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import ReplyKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import chat as ch
from apps.bot.avatars import Avatars
from apps.bot.keyboards import END_CHAT, NEW_CHAT, PARTNER_PROFILE
from backend.config.settings import BotSettings
from backend.modules.admin.settings import RuntimeSettings
from backend.modules.matching.matcher import Matcher
from backend.modules.matching.queue import SearchQueue
from backend.modules.users import service as users
from backend.modules.users.terms import TERMS_VERSION
from tests.support.chat import Chat, complete_onboarding
from tests.support.telegram import RecordingSession, make_bot

Sessions = async_sessionmaker[AsyncSession]
BOB_ID = 5353
MAIN_KEYS = [[NEW_CHAT]]
CHAT_KEYS = [[PARTNER_PROFILE], [END_CHAT]]


class World:
    """Alice and Bob on the bot, plus the worker's matcher with its own Telegram session."""

    def __init__(self, settings: BotSettings, redis: Redis, sessions: Sessions) -> None:
        self.alice = Chat(settings, redis, sessions)
        self.bob = Chat(settings, redis, sessions, user_id=BOB_ID)
        self.sessions = sessions
        self.queue = SearchQueue(redis)
        worker_bot, self.worker = make_bot()
        notifier = ch.ChatNotifier(worker_bot, sessions, Avatars(redis))
        self.matcher = Matcher(
            redis, sessions, RuntimeSettings(ttl_seconds=0), notifier, TERMS_VERSION
        )

    async def tick(self) -> None:
        await self.matcher.tick(datetime.now(UTC))

    async def user_id(self, telegram_id: int) -> int:
        async with self.sessions() as session:
            return (await users.get_or_create_user(session, telegram_id)).id


def keys(method: SendMessage | SendPhoto) -> list[list[str]] | None:
    markup = method.reply_markup
    if not isinstance(markup, ReplyKeyboardMarkup):
        return None
    return [[button.text for button in row] for row in markup.keyboard]


def last_to(session: RecordingSession, chat_id: int) -> SendMessage | SendPhoto:
    return session.sent_to(chat_id)[-1]


def text_of(method: SendMessage | SendPhoto) -> str:
    return method.text if isinstance(method, SendMessage) else method.caption or ""


async def match(world: World) -> None:
    assert await world.alice.send(NEW_CHAT) == ch.SEARCHING
    assert await world.bob.send(NEW_CHAT) == ch.SEARCHING
    await world.tick()


async def onboarded_world(settings: BotSettings, redis: Redis, sessions: Sessions) -> World:
    """Both users onboarded, with distinct nicknames so tests can tell whose card is whose."""
    world = World(settings, redis, sessions)
    await complete_onboarding(world.alice)
    await complete_onboarding(world.bob)
    assert await world.bob.send("/profile")
    await world.bob.press("✏️ Nickname")
    await world.bob.send("Moon Child")
    return world
