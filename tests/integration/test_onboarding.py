"""End-to-end onboarding through the simulated Telegram, against real PostgreSQL and Redis."""

import asyncio

import pytest
from aiogram.fsm.storage.redis import RedisEventIsolation
from redis.asyncio import Redis
from sqlalchemy import func, select

from apps.bot import onboarding as ob
from apps.bot.menu import HELP_TEXT
from backend.modules.users.models import Profile, TermsAcceptance
from backend.modules.users.terms import TERMS_VERSION
from tests.support.chat import (
    TERMS,
    USER_ID,
    Chat,
    Sessions,
    complete_required_steps,
    load_profile,
)
from tests.support.telegram import message_update


async def test_full_onboarding_using_only_the_bot(chat: Chat, session_factory: Sessions) -> None:
    await complete_required_steps(chat)
    assert await chat.send("Hyderabad") == ob.PROMPTS["photo"]
    assert await chat.send_photo("AgAC-large-photo") == ob.ONBOARDING_COMPLETE

    profile = await load_profile(session_factory)
    assert (profile.nickname, profile.gender, profile.country_code) == ("Night Owl", "female", "IN")
    assert profile.age_at_entry == 24
    assert profile.age_entered_on is not None  # exact date not compared: could cross midnight
    assert profile.city == "Hyderabad"
    assert profile.photo_file_id == "AgAC-large-photo"  # the largest size is kept
    assert profile.onboarding_completed_at is not None
    assert await chat.send("/start") == ob.WELCOME_BACK


async def test_optional_steps_can_be_skipped(chat: Chat, session_factory: Sessions) -> None:
    await complete_required_steps(chat)
    assert await chat.press("Skip") == ob.PROMPTS["photo"]
    assert await chat.press("Skip") == ob.ONBOARDING_COMPLETE

    profile = await load_profile(session_factory)
    assert profile.city is None and profile.photo_file_id is None
    assert profile.onboarding_completed_at is not None


async def test_terms_must_be_accepted_before_any_profile_data(
    chat: Chat, session_factory: Sessions
) -> None:
    assert await chat.send("/start") == TERMS
    assert await chat.send("Night Owl") == ob.TERMS_REQUIRED
    assert await chat.press("Decline") == ob.TERMS_DECLINED
    assert await chat.send("/start") == TERMS

    profile = await load_profile(session_factory)
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
    assert (await load_profile(session_factory)).age_at_entry is None

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
    assert (await load_profile(session_factory)).country_code is None


async def test_commands_are_not_taken_as_answers(chat: Chat, session_factory: Sessions) -> None:
    await chat.send("/start")
    await chat.press("I agree")

    assert await chat.send("/settings") == ob.PROMPTS["nickname"]  # unknown command
    assert await chat.send("/help") == HELP_TEXT  # help works mid-onboarding
    assert (await load_profile(session_factory)).nickname is None


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
    assert (await load_profile(session_factory)).age_at_entry is None
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
    profile = await load_profile(session_factory)
    assert acceptances == 1
    assert profile.gender == "female"
    assert chat.session.sent_texts().count(ob.PROMPTS["nickname"]) == 1
    assert chat.last() == ob.PROMPTS["country"]


async def test_skip_still_works_if_fsm_state_was_lost(
    chat: Chat, redis: Redis, session_factory: Sessions
) -> None:
    await complete_required_steps(chat)
    skip_city = chat.session.last_buttons()["Skip"]
    await redis.flushdb()

    await chat.press_data(skip_city)

    assert chat.last() == ob.PROMPTS["photo"]


async def test_button_for_an_older_terms_version_cannot_accept(
    chat: Chat, session_factory: Sessions
) -> None:
    await chat.send("/start")
    old_agree = ob.TermsChoice(version="draft-0", accept=True).pack()

    await chat.press_data(old_agree)

    assert chat.last() == TERMS  # the current terms are shown again
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TermsAcceptance)) == 0


async def test_terms_update_mid_onboarding_blocks_profile_data(
    chat: Chat, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    await chat.send("/start")
    await chat.press("I agree")  # waiting for the nickname now

    monkeypatch.setattr(ob, "TERMS_VERSION", "draft-99")  # the terms change
    reply = await chat.send("Night Owl")

    assert reply == TERMS
    assert (await load_profile(session_factory)).nickname is None


async def test_onboarding_is_ignored_in_group_chats(chat: Chat, session_factory: Sessions) -> None:
    await chat.dispatcher.feed_update(
        chat.bot, message_update(9001, USER_ID, "/start", chat_type="group")
    )
    await chat.dispatcher.feed_update(
        chat.bot, message_update(9002, USER_ID, "Night Owl", chat_type="supergroup")
    )

    assert chat.session.sent_texts() == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Profile)) == 0


async def test_photo_is_kept_even_if_fsm_state_was_lost(
    chat: Chat, redis: Redis, session_factory: Sessions
) -> None:
    await complete_required_steps(chat)
    assert await chat.press("Skip") == ob.PROMPTS["photo"]
    await redis.flushdb()

    assert await chat.send_photo("AgAC-after-state-loss") == ob.ONBOARDING_COMPLETE
    assert (await load_profile(session_factory)).photo_file_id == "AgAC-after-state-loss"


async def test_quick_successive_messages_are_processed_in_order(
    chat: Chat, session_factory: Sessions
) -> None:
    await chat.send("/start")
    await chat.press("I agree")

    # Both arrive before the first has been handled. Without per-user isolation the
    # second would also be routed by the "nickname" state and overwrite the nickname.
    await asyncio.gather(
        chat.dispatcher.feed_update(chat.bot, message_update(9101, USER_ID, "Night Owl")),
        chat.dispatcher.feed_update(chat.bot, message_update(9102, USER_ID, "24")),
    )

    profile = await load_profile(session_factory)
    assert profile.nickname == "Night Owl"
    assert profile.age_at_entry == 24


async def test_skip_after_a_terms_update_shows_the_new_terms(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    await complete_required_steps(chat)
    skip_city = chat.session.last_buttons()["Skip"]
    await chat.press("Skip")  # now on the photo step; the city Skip button is stale

    monkeypatch.setattr(ob, "TERMS_VERSION", "draft-99")  # the terms change
    await chat.press_data(skip_city)

    assert chat.last() == TERMS


async def test_event_isolation_locks_are_kept_in_redis_and_released(
    chat: Chat, redis: Redis
) -> None:
    assert isinstance(chat.dispatcher.fsm.events_isolation, RedisEventIsolation)

    await chat.send("/start")

    # The per-user lock is released after the update; nothing is left behind.
    assert await redis.keys("*:lock") == []
