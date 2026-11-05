"""Paid matching with a chosen gender (D-050)."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import SendPhoto
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import chat as ch
from apps.bot import onboarding as ob
from apps.bot.endings import MEET_PROMPT, RANDOM_BUTTON, SearchAction
from apps.bot.keyboards import NEW_CHAT
from backend.config.settings import BotSettings
from backend.modules.admin import service as admin
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.chat.models import Conversation
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinHold, CoinTransaction
from backend.modules.users.models import User
from tests.support.chat import TERMS, USER_ID, Chat
from tests.support.world import World, onboarded_world, text_of

Sessions = async_sessionmaker[AsyncSession]
CARL_ID = 7171
MEN = "👨 Men · 2 coins"
WOMEN = "👩 Women · 2 coins"


async def onboard_as(chat: Chat, gender: str) -> None:
    assert await chat.send("/start") == TERMS
    assert await chat.press("I agree") == ob.PROMPTS["nickname"]
    assert await chat.send(f"User {chat.user_id}") == ob.PROMPTS["age"]
    assert await chat.send("30") == ob.PROMPTS["gender"]
    assert await chat.press(gender) == ob.PROMPTS["country"]
    assert await chat.send("india") == ob.COUNTRY_PICK
    assert await chat.press("India") == ob.PROMPTS["city"]
    assert await chat.press("Skip") == ob.PROMPTS["photo"]
    assert await chat.press("Skip") == ob.ONBOARDING_COMPLETE


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    # Alice and Bob are women; both start with the 10-coin welcome bonus.
    return await onboarded_world(bot_settings, redis, session_factory)


@pytest.fixture
async def carl(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory, user_id=CARL_ID)
    await onboard_as(chat, "Male")
    return chat


async def choose(chat: Chat, button: str) -> str:
    assert await chat.send(NEW_CHAT) == MEET_PROMPT
    return await chat.press(button)


async def balance(sessions: Sessions, telegram_id: int) -> coins.Balance:
    async with sessions() as session:
        user_id = await session.scalar(select(User.id).where(User.telegram_user_id == telegram_id))
        assert user_id is not None
        return await coins.balance(session, user_id)


async def open_chats(sessions: Sessions) -> int:
    async with sessions() as session:
        rows = await session.execute(select(Conversation).where(Conversation.ended_at.is_(None)))
        return len(rows.all())


async def paid_entries(sessions: Sessions) -> list[int]:
    async with sessions() as session:
        rows = await session.execute(
            select(CoinTransaction.amount).where(CoinTransaction.kind == "paid_match")
        )
        return list(rows.scalars())


# --- Starting a paid search ------------------------------------------------------------


async def test_the_menu_offers_paid_chats_at_the_set_price(
    world: World, session_factory: Sessions
) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.PAID_MATCH_COINS.value, value=5))
        await session.commit()
    assert await world.alice.send(NEW_CHAT) == MEET_PROMPT
    assert list(world.alice.session.last_buttons()) == [
        RANDOM_BUTTON,
        "👨 Men · 5 coins",
        "👩 Women · 5 coins",
        "🧑 Other genders · 5 coins",
    ]


async def test_a_paid_search_holds_the_price(world: World, session_factory: Sessions) -> None:
    reply = await choose(world.alice, MEN)
    assert reply == ch.SEARCHING_FOR.format(who="a man", price="2 coins")
    assert await balance(session_factory, USER_ID) == coins.Balance(10, 2)


async def test_not_enough_coins_starts_nothing(world: World, session_factory: Sessions) -> None:
    async with session_factory() as session:
        await admin.adjust_coins(session, USER_ID, -9, "test")
        await session.commit()
    reply = await choose(world.alice, MEN)
    assert reply == ch.NOT_ENOUGH_COINS.format(price="2 coins", available="1 coin")
    assert await world.queue.current(await world.user_id(USER_ID)) is None
    assert (await balance(session_factory, USER_ID)).held == 0


async def test_a_second_tap_keeps_one_search_and_one_hold(
    world: World, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)
    assert await choose(world.alice, MEN) == ch.ALREADY_SEARCHING
    assert await choose(world.alice, RANDOM_BUTTON) == ch.ALREADY_SEARCHING
    assert (await balance(session_factory, USER_ID)).held == 2


async def test_an_unknown_choice_does_nothing(world: World, session_factory: Sessions) -> None:
    await world.alice.press_data(SearchAction(action="start", wants="robots").pack())
    assert await world.queue.current(await world.user_id(USER_ID)) is None


# --- Matching --------------------------------------------------------------------------


async def test_a_paid_search_meets_a_fitting_free_searcher_and_only_the_payer_pays(
    world: World, carl: Chat, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)
    assert await choose(carl, RANDOM_BUTTON) == ch.SEARCHING
    await world.tick()
    assert await open_chats(session_factory) == 1
    assert await paid_entries(session_factory) == [-2]
    assert await balance(session_factory, USER_ID) == coins.Balance(8, 0)
    assert await balance(session_factory, CARL_ID) == coins.Balance(10, 0)
    cards = [m for m in world.worker.requests if isinstance(m, SendPhoto)]
    to_alice = next(m for m in cards if int(m.chat_id) == USER_ID)
    to_carl = next(m for m in cards if int(m.chat_id) == CARL_ID)
    assert text_of(to_alice).startswith(f"{ch.MATCHED}\n{ch.COINS_USED.format(coins='2 coins')}")
    assert "coins" not in text_of(to_carl).split("\n\n")[0]  # Carl never learns who paid


async def test_a_paid_search_never_meets_the_wrong_gender(
    world: World, carl: Chat, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)
    await choose(world.bob, RANDOM_BUTTON)  # a woman
    await world.tick()
    assert await open_chats(session_factory) == 0
    await choose(carl, RANDOM_BUTTON)
    await world.tick()
    assert await open_chats(session_factory) == 1
    assert await balance(session_factory, USER_ID) == coins.Balance(8, 0)


async def test_each_must_fit_the_others_choice(
    world: World, carl: Chat, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)  # Alice, a woman, wants a man
    await choose(carl, "👨 Men · 2 coins")  # Carl, a man, also wants a man
    await world.tick()
    assert await open_chats(session_factory) == 0


async def test_when_both_chose_both_pay(
    world: World, carl: Chat, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)
    await choose(carl, WOMEN)
    await world.tick()
    assert await open_chats(session_factory) == 1
    assert sorted(await paid_entries(session_factory)) == [-2, -2]


# --- Coins back when there is no match -------------------------------------------------


async def test_cancelling_returns_the_coins(world: World, session_factory: Sessions) -> None:
    await choose(world.alice, MEN)
    assert await world.alice.press(ch.CANCEL_BUTTON) == f"{ch.SEARCH_CANCELLED} {ch.COINS_RETURNED}"
    assert await balance(session_factory, USER_ID) == coins.Balance(10, 0)


async def test_a_search_that_finds_no_one_returns_the_coins(
    world: World, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)
    await world.matcher.tick(datetime.now(UTC) + timedelta(minutes=3))
    assert await balance(session_factory, USER_ID) == coins.Balance(10, 0)
    expired = [text_of(m) for m in world.worker.sent_to(USER_ID)]
    assert f"{ch.NO_ONE_FOUND} {ch.COINS_RETURNED}\n\n{MEET_PROMPT}" in expired


async def test_a_searcher_who_is_no_longer_eligible_gets_the_coins_back(
    world: World, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status="suspended")
        )
        await session.commit()
    await world.tick()
    assert await balance(session_factory, USER_ID) == coins.Balance(10, 0)


async def test_no_match_is_made_if_the_coins_cannot_be_taken(
    world: World, carl: Chat, session_factory: Sessions
) -> None:
    await choose(world.alice, MEN)
    async with session_factory() as session:  # the hold was released meanwhile
        hold = (await session.execute(select(CoinHold))).scalar_one()
        await coins.release(session, hold.id, datetime.now(UTC))
        await session.commit()
    await choose(carl, RANDOM_BUTTON)
    await world.tick()
    assert await open_chats(session_factory) == 0
    assert await paid_entries(session_factory) == []
    # Alice's search ended; Carl is still searching.
    assert await world.queue.current(await world.user_id(USER_ID)) is None
    assert await world.queue.current(await world.user_id(CARL_ID)) is not None


async def test_stale_holds_are_released(world: World, session_factory: Sessions) -> None:
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:  # held, but its search never started
        await coins.hold(session, alice, 2, "paid_match", "search:orphan")
        await session.commit()
    await world.matcher.tick(datetime.now(UTC) + timedelta(minutes=5))
    assert (await balance(session_factory, USER_ID)).held == 2  # not stale yet
    await world.matcher.tick(datetime.now(UTC) + timedelta(minutes=13))
    assert (await balance(session_factory, USER_ID)).held == 0


async def test_free_matching_is_unchanged(world: World, session_factory: Sessions) -> None:
    await choose(world.alice, RANDOM_BUTTON)
    await choose(world.bob, RANDOM_BUTTON)
    await world.tick()
    assert await open_chats(session_factory) == 1
    assert await paid_entries(session_factory) == []  # both free
