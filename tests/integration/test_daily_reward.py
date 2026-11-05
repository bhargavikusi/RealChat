"""The daily promotional reward (D-050)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import EditMessageReplyMarkup
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import coins as bot_coins
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.coins import rewards
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.users import service as users
from tests.support.chat import USER_ID, Chat, complete_onboarding

Sessions = async_sessionmaker[AsyncSession]
CLAIM = bot_coins.DailyAction(action="claim").pack()
NOW = datetime(2026, 11, 6, 23, 59, tzinfo=UTC)


async def _set(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        await session.merge(AppSetting(key=key.value, value=value))
        await session.commit()


async def _daily(sessions: Sessions) -> list[int]:
    async with sessions() as session:
        rows = await session.execute(
            select(CoinTransaction.amount).where(CoinTransaction.kind == "daily_reward")
        )
        return list(rows.scalars())


@pytest.fixture
async def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    return chat


async def test_it_is_off_by_default(chat: Chat, session_factory: Sessions) -> None:
    shown = await chat.send("/coins")
    assert "Daily reward" not in shown
    assert chat.session.sent_to(USER_ID)[-1].reply_markup is None
    await chat.press_data(CLAIM)  # an old button
    assert chat.session.callback_answers()[-1] == bot_coins.DAILY_OFF
    assert await _daily(session_factory) == []


async def test_it_can_be_claimed_once_a_day_while_on(chat: Chat, session_factory: Sessions) -> None:
    await _set(session_factory, SettingKey.DAILY_REWARD_ON, 1)
    shown = await chat.send("/coins")
    assert bot_coins.DAILY_AVAILABLE.format(amount="1 coin") in shown.splitlines()
    assert await chat.press(bot_coins.CLAIM_BUTTON) == bot_coins.CLAIMED.format(amount="1 coin")
    assert [m for m in chat.session.requests if isinstance(m, EditMessageReplyMarkup)]
    assert await _daily(session_factory) == [1]
    # Claimed: /coins says so and offers no button; an old button does nothing.
    shown = await chat.send("/coins")
    assert bot_coins.DAILY_CLAIMED in shown.splitlines()
    assert chat.session.sent_to(USER_ID)[-1].reply_markup is None
    await chat.press_data(CLAIM)
    assert chat.session.callback_answers()[-1] == bot_coins.ALREADY_CLAIMED
    assert await _daily(session_factory) == [1]


async def test_the_amount_is_read_at_claim_time(chat: Chat, session_factory: Sessions) -> None:
    await _set(session_factory, SettingKey.DAILY_REWARD_ON, 1)
    await chat.send("/coins")  # offered while the amount was 1
    await _set(session_factory, SettingKey.DAILY_REWARD_COINS, 3)
    assert await chat.press(bot_coins.CLAIM_BUTTON) == bot_coins.CLAIMED.format(amount="3 coins")
    assert await _daily(session_factory) == [3]


async def test_switching_it_off_stops_claims(chat: Chat, session_factory: Sessions) -> None:
    await _set(session_factory, SettingKey.DAILY_REWARD_ON, 1)
    await chat.send("/coins")
    await _set(session_factory, SettingKey.DAILY_REWARD_ON, 0)
    await chat.press(bot_coins.CLAIM_BUTTON)
    assert chat.session.callback_answers()[-1] == bot_coins.DAILY_OFF
    assert await _daily(session_factory) == []


async def test_users_who_have_not_finished_signing_up_cannot_claim(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    await _set(session_factory, SettingKey.DAILY_REWARD_ON, 1)
    chat = Chat(bot_settings, redis, session_factory)
    await chat.press_data(CLAIM)
    assert chat.session.callback_answers()[-1] == bot_coins.NOT_AVAILABLE
    assert await _daily(session_factory) == []


async def _user_id(sessions: Sessions) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await session.commit()
        return user.id


async def _claim(sessions: Sessions, user_id: int, now: datetime) -> bool:
    async with sessions() as session:
        done = await rewards.claim_daily(session, user_id, now, 1)
        await session.commit()
        return done


async def test_a_new_utc_day_can_be_claimed_again(session_factory: Sessions) -> None:
    user_id = await _user_id(session_factory)
    assert await _claim(session_factory, user_id, NOW)
    assert not await _claim(session_factory, user_id, NOW)
    assert await _claim(session_factory, user_id, NOW + timedelta(minutes=2))  # past midnight
    async with session_factory() as session:
        assert (await coins.balance(session, user_id)).total == 2


async def test_simultaneous_claims_grant_once(session_factory: Sessions) -> None:
    user_id = await _user_id(session_factory)
    results = await asyncio.gather(*(_claim(session_factory, user_id, NOW) for _ in range(5)))
    assert sorted(results) == [False, False, False, False, True]
    assert await _daily(session_factory) == [1]
