"""Alice and Bob on the bot plus the worker's matcher, for two-user chat tests."""

from datetime import UTC, datetime

from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import chat as ch
from apps.bot.avatars import Avatars
from apps.bot.endings import (
    BLOCK_BUTTON,
    MEET_PROMPT,
    RANDOM_BUTTON,
    REPORT_BUTTON,
    SAFETY_PROMPT,
    UNBLOCK_BUTTON,
)
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
        self.worker_bot, self.worker = make_bot()
        self.notifier = ch.ChatNotifier(self.worker_bot, sessions, Avatars(redis))
        self.matcher = Matcher(
            redis, sessions, RuntimeSettings(ttl_seconds=0), self.notifier, TERMS_VERSION
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


def buttons(method: SendMessage | SendPhoto) -> list[str]:
    """The text of a message's inline buttons, in order."""
    markup = method.reply_markup
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


def assert_chat_ended(
    session: RecordingSession, chat_id: int, how: str, *, blocked: bool = False
) -> None:
    """The last three messages to `chat_id`: how the chat ended (with the main keyboard),
    Block or Unblock and Report, then the new-chat menu."""
    ended, safety, menu = session.sent_to(chat_id)[-3:]
    assert (text_of(ended), keys(ended)) == (how, MAIN_KEYS)
    toggle = UNBLOCK_BUTTON if blocked else BLOCK_BUTTON
    assert (text_of(safety), buttons(safety)) == (SAFETY_PROMPT, [toggle, REPORT_BUTTON])
    assert (text_of(menu), buttons(menu)) == (MEET_PROMPT, [RANDOM_BUTTON])


def last_to(session: RecordingSession, chat_id: int) -> SendMessage | SendPhoto:
    return session.sent_to(chat_id)[-1]


def text_of(method: SendMessage | SendPhoto) -> str:
    return method.text if isinstance(method, SendMessage) else method.caption or ""


async def search(chat: Chat) -> str:
    """Tap 🎭 New Anonymous Chat, then the free random chat; returns the reply."""
    assert await chat.send(NEW_CHAT) == MEET_PROMPT
    return await chat.press(RANDOM_BUTTON)


async def match(world: World) -> None:
    assert await search(world.alice) == ch.SEARCHING
    assert await search(world.bob) == ch.SEARCHING
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
