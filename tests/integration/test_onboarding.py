"""End-to-end onboarding through the simulated Telegram, against real PostgreSQL and Redis."""

from collections.abc import AsyncIterator
from datetime import date
from itertools import count

import pytest
from aiogram import Dispatcher
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import onboarding as ob
from apps.bot.main import create_dispatcher
from backend.config.settings import BotSettings
from backend.core.redis import create_redis
from backend.modules.users import service
from backend.modules.users.models import Profile, TermsAcceptance
from backend.modules.users.terms import TERMS_VERSION, terms_text
from tests.support.telegram import (
    FAKE_TOKEN,
    RecordingSession,
    callback_update,
    make_bot,
    message_update,
    photo_update,
)

Sessions = async_sessionmaker[AsyncSession]
USER_ID = 4242
TERMS = terms_text(18)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    # TEST_REDIS_URL points at a database used only by tests, so flushing it is safe.
    client = create_redis(redis_url)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def bot_settings(database_url: str, redis_url: str) -> BotSettings:
    return BotSettings(
        _env_file=None,
        app_env="test",
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
        telegram_bot_token=SecretStr(FAKE_TOKEN),
    )


class Chat:
    """One user talking to the bot. `restart()` simulates a bot process restart."""

    _ids = count(1)

    def __init__(self, settings: BotSettings, redis: Redis, sessions: Sessions) -> None:
        self._settings, self._redis, self._sessions = settings, redis, sessions
        self.bot, self.telegram = make_bot()
        self.dispatcher: Dispatcher = create_dispatcher(settings, redis, sessions)

    def restart(self) -> None:
        self.bot, self.telegram = make_bot()
        self.dispatcher = create_dispatcher(self._settings, self._redis, self._sessions)

    async def send(self, text: str) -> str:
        await self.dispatcher.feed_update(self.bot, message_update(next(self._ids), USER_ID, text))
        return self.last()

    async def press(self, button: str) -> str:
        data = self.telegram.last_buttons()[button]
        await self.dispatcher.feed_update(self.bot, callback_update(next(self._ids), USER_ID, data))
        return self.last()

    async def press_data(self, data: str) -> None:
        await self.dispatcher.feed_update(self.bot, callback_update(next(self._ids), USER_ID, data))

    async def send_photo(self, file_id: str) -> str:
        await self.dispatcher.feed_update(self.bot, photo_update(next(self._ids), USER_ID, file_id))
        return self.last()

    def last(self) -> str:
        texts = self.telegram.sent_texts()
        return texts[-1] if texts else ""

    @property
    def session(self) -> RecordingSession:
        return self.telegram


@pytest.fixture
def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    return Chat(bot_settings, redis, session_factory)


async def _profile(sessions: Sessions) -> Profile:
    async with sessions() as session:
        user = await service.get_or_create_user(session, USER_ID)
        return await service.get_profile(session, user.id)


async def _complete_required_steps(chat: Chat) -> None:
    assert await chat.send("/start") == TERMS
    assert await chat.press("I agree") == ob.PROMPTS["nickname"]
    assert await chat.send("Night Owl") == ob.PROMPTS["age"]
    assert await chat.send("24") == ob.PROMPTS["gender"]
    assert await chat.press("Female") == ob.PROMPTS["country"]
    assert await chat.send("india") == ob.COUNTRY_PICK
    assert await chat.press("India") == ob.PROMPTS["city"]


async def test_full_onboarding_using_only_the_bot(chat: Chat, session_factory: Sessions) -> None:
    await _complete_required_steps(chat)
    assert await chat.send("Hyderabad") == ob.PROMPTS["photo"]
    assert await chat.send_photo("AgAC-large-photo") == ob.ONBOARDING_COMPLETE

    profile = await _profile(session_factory)
    assert (profile.nickname, profile.gender, profile.country_code) == ("Night Owl", "female", "IN")
    assert (profile.age_at_entry, profile.age_entered_on) == (24, date.today())
    assert profile.city == "Hyderabad"
    assert profile.photo_file_id == "AgAC-large-photo"  # the largest size is kept
    assert profile.onboarding_completed_at is not None
    assert await chat.send("/start") == ob.WELCOME_BACK


async def test_optional_steps_can_be_skipped(chat: Chat, session_factory: Sessions) -> None:
    await _complete_required_steps(chat)
    assert await chat.press("Skip") == ob.PROMPTS["photo"]
    assert await chat.press("Skip") == ob.ONBOARDING_COMPLETE

    profile = await _profile(session_factory)
    assert profile.city is None and profile.photo_file_id is None
    assert profile.onboarding_completed_at is not None


async def test_terms_must_be_accepted_before_any_profile_data(
    chat: Chat, session_factory: Sessions
) -> None:
    assert await chat.send("/start") == TERMS
    assert await chat.send("Night Owl") == ob.TERMS_REQUIRED
    assert await chat.press("Decline") == ob.TERMS_DECLINED
    assert await chat.send("/start") == TERMS

    profile = await _profile(session_factory)
    assert profile.nickname is None

    assert await chat.press("I agree") == ob.PROMPTS["nickname"]
    async with session_factory() as session:
        accepted = (await session.execute(select(TermsAcceptance))).scalar_one()
    assert accepted.terms_version == TERMS_VERSION
    assert accepted.accepted_at is not None


async def test_under_age_user_is_refused_and_may_correct_a_mistake(
    chat: Chat, session_factory: Sessions
) -> None:
    await chat.send("/start")
    await chat.press("I agree")
    await chat.send("Night Owl")

    reply = await chat.send("16")
    assert reply.startswith("You must be at least 18 to use RealChat.")
    assert (await _profile(session_factory)).age_at_entry is None

    assert await chat.send("19") == ob.PROMPTS["gender"]  # D-017: retry allowed


@pytest.mark.parametrize(
    "step_inputs,bad,expected_start",
    [
        ([], "see t.me/spam", "Nickname can't contain links."),
        ([], "A", "Nickname must be 2-20 characters."),
        (["Night Owl"], "twenty", "Please send your age as a number"),
        (["Night Owl"], "120", "Please enter your real age."),
    ],
)
async def test_invalid_input_is_explained_and_can_be_retried(
    chat: Chat, step_inputs: list[str], bad: str, expected_start: str
) -> None:
    await chat.send("/start")
    await chat.press("I agree")
    for text in step_inputs:
        await chat.send(text)

    reply = await chat.send(bad)

    assert reply.startswith(expected_start)
    assert bad not in reply  # never echoes the input


async def test_unknown_country_and_none_of_these(chat: Chat, session_factory: Sessions) -> None:
    await chat.send("/start")
    await chat.press("I agree")
    await chat.send("Night Owl")
    await chat.send("24")
    await chat.press("Male")

    assert await chat.send("Atlantis") == ob.COUNTRY_NOT_FOUND
    assert await chat.send("korea") == ob.COUNTRY_PICK
    assert set(chat.session.last_buttons()) == {"North Korea", "South Korea", "None of these"}
    assert await chat.press("None of these") == ob.PROMPTS["country"]
    assert (await _profile(session_factory)).country_code is None


async def test_commands_are_not_taken_as_answers(chat: Chat, session_factory: Sessions) -> None:
    await chat.send("/start")
    await chat.press("I agree")

    assert await chat.send("/help") == ob.PROMPTS["nickname"]
    assert (await _profile(session_factory)).nickname is None


async def test_resumes_at_the_right_step_after_a_restart(
    chat: Chat, redis: Redis, session_factory: Sessions
) -> None:
    await chat.send("/start")
    await chat.press("I agree")
    await chat.send("Night Owl")

    chat.restart()
    assert await chat.send("/start") == ob.PROMPTS["age"]

    # Even if Redis lost the FSM state, required steps resume from the database.
    await redis.flushdb()
    chat.restart()
    # With no FSM state the text is not taken as an answer; the age step is re-asked.
    assert await chat.send("24") == ob.PROMPTS["age"]
    assert (await _profile(session_factory)).age_at_entry is None
    assert await chat.send("24") == ob.PROMPTS["gender"]


async def test_repeated_button_presses_do_not_duplicate_or_skip(
    chat: Chat, session_factory: Sessions
) -> None:
    await chat.send("/start")
    agree = chat.session.last_buttons()["I agree"]
    await chat.press_data(agree)
    await chat.press_data(agree)  # double tap
    await chat.send("Night Owl")
    await chat.send("24")
    female = chat.session.last_buttons()["Female"]
    male = chat.session.last_buttons()["Male"]
    await chat.press_data(female)
    await chat.press_data(male)  # stale button after the step was answered

    async with session_factory() as session:
        acceptances = await session.scalar(select(func.count()).select_from(TermsAcceptance))
    profile = await _profile(session_factory)
    assert acceptances == 1
    assert profile.gender == "female"
    assert chat.session.sent_texts().count(ob.PROMPTS["nickname"]) == 1
    assert chat.last() == ob.PROMPTS["country"]


async def test_skip_still_works_if_fsm_state_was_lost(
    chat: Chat, redis: Redis, session_factory: Sessions
) -> None:
    await _complete_required_steps(chat)
    skip_city = chat.session.last_buttons()["Skip"]
    await redis.flushdb()

    await chat.press_data(skip_city)

    assert chat.last() == ob.PROMPTS["photo"]
