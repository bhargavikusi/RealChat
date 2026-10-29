"""Two users finding each other through the bot and the worker's matcher (D-027 to D-033)."""

from datetime import UTC, datetime

import pytest
from aiogram.methods import SendPhoto
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import admin as ad
from apps.bot import chat as ch
from apps.bot import onboarding as ob
from apps.bot.keyboards import END_CHAT, NEW_CHAT, PARTNER_PROFILE
from backend.config.settings import BotSettings
from backend.modules.admin import service as admin_service
from backend.modules.admin.settings import SPECS, SettingKey
from backend.modules.chat import service as chat_service
from tests.support.chat import TERMS, USER_ID, Chat
from tests.support.telegram import message_update
from tests.support.world import (
    BOB_ID,
    CHAT_KEYS,
    MAIN_KEYS,
    World,
    keys,
    last_to,
    match,
    onboarded_world,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    return await onboarded_world(bot_settings, redis, session_factory)


# --- Keyboards -----------------------------------------------------------------------


async def test_onboarding_ends_with_the_main_keyboard(world: World) -> None:
    done = [m for m in world.alice.session.sent_to(USER_ID) if text_of(m) == ob.ONBOARDING_COMPLETE]
    assert keys(done[-1]) == MAIN_KEYS
    await world.alice.send("hello")
    assert keys(last_to(world.alice.session, USER_ID)) == MAIN_KEYS


# --- Searching -----------------------------------------------------------------------


async def test_two_users_are_matched_and_see_each_other(world: World) -> None:
    await match(world)
    to_alice = last_to(world.worker, USER_ID)
    to_bob = last_to(world.worker, BOB_ID)
    assert isinstance(to_alice, SendPhoto) and isinstance(to_bob, SendPhoto)
    # Each receives the other's card.
    assert text_of(to_alice).startswith(f"{ch.MATCHED}\n\n👤 Moon Child")
    assert text_of(to_bob).startswith(f"{ch.MATCHED}\n\n👤 Night Owl")
    assert keys(to_alice) == keys(to_bob) == CHAT_KEYS

    # While in a chat, the keyboard stays the chat keyboard and 🎭 does not search.
    assert await world.alice.send(NEW_CHAT) == ch.ALREADY_IN_CHAT
    await world.alice.send("/start")
    assert keys(last_to(world.alice.session, USER_ID)) == CHAT_KEYS


async def test_partner_profile_shows_the_partner_card(world: World) -> None:
    await match(world)
    assert (await world.alice.send(PARTNER_PROFILE)).startswith(
        f"{ch.PARTNER_HEADER}\n\n👤 Moon Child"
    )
    assert await world.alice.send(END_CHAT) == ch.YOU_ENDED
    assert await world.alice.send(PARTNER_PROFILE) == ch.NOT_IN_CHAT


async def test_repeated_taps_and_duplicate_updates_make_one_search(
    world: World, bot_settings: BotSettings
) -> None:
    assert await world.alice.send(NEW_CHAT) == ch.SEARCHING
    cancel = world.alice.session.last_buttons()[ch.CANCEL_BUTTON]
    assert await world.alice.send(NEW_CHAT) == ch.ALREADY_SEARCHING
    assert world.alice.session.last_buttons()[ch.CANCEL_BUTTON] == cancel

    duplicate = message_update(990_001, USER_ID, NEW_CHAT)
    await world.alice.dispatcher.feed_update(world.alice.bot, duplicate)
    sent = len(world.alice.session.requests)
    await world.alice.dispatcher.feed_update(world.alice.bot, duplicate)
    assert len(world.alice.session.requests) == sent  # the redelivery was dropped

    alice = await world.user_id(USER_ID)
    assert [user for user, _ in await world.queue.waiting(10, started_after=0)] == [alice]


async def test_cancelling_a_search(world: World) -> None:
    await world.alice.send(NEW_CHAT)
    cancel = world.alice.session.last_buttons()[ch.CANCEL_BUTTON]
    assert await world.alice.press(ch.CANCEL_BUTTON) == ch.SEARCH_CANCELLED
    await world.alice.press_data(cancel)
    assert world.alice.session.callback_answers()[-1] == ch.SEARCH_ENDED

    await world.bob.send(NEW_CHAT)
    await world.tick()
    assert world.worker.sent_to(USER_ID) == []  # alice was not matched


async def test_an_old_cancel_button_cannot_cancel_a_new_search(world: World) -> None:
    await world.alice.send(NEW_CHAT)
    old_cancel = world.alice.session.last_buttons()[ch.CANCEL_BUTTON]
    await world.alice.press(ch.CANCEL_BUTTON)
    await world.alice.send(NEW_CHAT)
    await world.alice.press_data(old_cancel)
    assert world.alice.session.callback_answers()[-1] == ch.SEARCH_ENDED
    assert len(await world.queue.waiting(10, started_after=0)) == 1


async def test_cancel_after_a_match_changes_nothing(world: World) -> None:
    await world.alice.send(NEW_CHAT)
    cancel = world.alice.session.last_buttons()[ch.CANCEL_BUTTON]
    await world.bob.send(NEW_CHAT)
    await world.tick()
    await world.alice.press_data(cancel)
    assert world.alice.session.callback_answers()[-1] == ch.SEARCH_ENDED
    async with world.sessions() as session:
        assert await chat_service.active_chat(session, await world.user_id(USER_ID))


async def test_an_expired_search_offers_to_try_again(
    world: World, session_factory: Sessions
) -> None:
    alice = await world.user_id(USER_ID)
    await world.queue.start(alice, datetime.now(UTC).timestamp() - 600)
    await world.tick()
    notice = last_to(world.worker, USER_ID)
    assert text_of(notice) == ch.NO_ONE_FOUND
    world.alice.session.requests.append(notice)  # make its button pressable
    assert await world.alice.press(ch.TRY_AGAIN_BUTTON) == ch.SEARCHING


async def test_users_who_have_not_onboarded_are_shown_the_terms(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    newcomer = Chat(bot_settings, redis, session_factory, user_id=6161)
    assert await newcomer.send(NEW_CHAT) == TERMS


# --- Ending --------------------------------------------------------------------------


async def test_ending_a_chat_tells_both_and_restores_the_main_keyboard(world: World) -> None:
    await match(world)
    assert await world.alice.send(END_CHAT) == ch.YOU_ENDED
    assert keys(last_to(world.alice.session, USER_ID)) == MAIN_KEYS
    to_bob = last_to(world.alice.session, BOB_ID)
    assert (text_of(to_bob), keys(to_bob)) == (ch.PARTNER_ENDED, MAIN_KEYS)

    assert await world.bob.send(END_CHAT) == ch.NOT_IN_CHAT
    async with world.sessions() as session:
        assert await chat_service.active_chat(session, await world.user_id(BOB_ID)) is None


async def test_both_can_search_again_after_a_chat(world: World) -> None:
    await match(world)
    await world.alice.send(END_CHAT)
    await match(world)  # nobody else is waiting, so they may meet again (D-033)
    async with world.sessions() as session:
        assert await chat_service.active_chat(session, await world.user_id(USER_ID))


async def test_deleting_the_account_ends_the_chat(world: World) -> None:
    await match(world)
    await world.alice.send("/profile")
    await world.alice.press(ac.ADVANCED_SETTINGS)
    await world.alice.press(ac.DELETE_BUTTON)
    await world.alice.press(ac.CONFIRM_BUTTON)
    assert text_of(last_to(world.alice.session, USER_ID)) == ac.DELETED
    to_bob = last_to(world.alice.session, BOB_ID)
    assert (text_of(to_bob), keys(to_bob)) == (ac.PARTNER_LEFT, MAIN_KEYS)
    # The partner is told before the deleting user's own reply.
    requests = world.alice.session.requests
    assert requests.index(to_bob) < requests.index(last_to(world.alice.session, USER_ID))
    async with world.sessions() as session:
        assert await chat_service.active_chat(session, await world.user_id(BOB_ID)) is None


async def test_new_chat_works_during_an_admin_edit(world: World) -> None:
    async with world.sessions() as session:
        await admin_service.set_role(session, USER_ID, "admin")
        await session.commit()
    await world.alice.send("/profile")
    await world.alice.press(ac.ADVANCED_SETTINGS)
    await world.alice.press(ad.ADMIN_BUTTON)
    await world.alice.press(SPECS[SettingKey.CHAT_RETENTION_DAYS].label)
    assert await world.alice.send(NEW_CHAT) == ch.SEARCHING  # not taken as a value
