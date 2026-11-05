"""The welcome bonus and invite rewards (D-050)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import onboarding as ob
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.coins import rewards
from backend.modules.coins import service as coins
from backend.modules.coins.models import Referral
from backend.modules.users import service as users
from backend.modules.users.models import User
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked
from tests.support.world import text_of

Sessions = async_sessionmaker[AsyncSession]
BOB_ID = 5353
CAROL_ID = 6464
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


async def _user_id(sessions: Sessions, telegram_id: int) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        await session.commit()
        return user.id


async def _total(sessions: Sessions, telegram_id: int) -> int:
    user_id = await _user_id(sessions, telegram_id)
    async with sessions() as session:
        return (await coins.balance(session, user_id)).total


async def _code(sessions: Sessions, telegram_id: int) -> str:
    user_id = await _user_id(sessions, telegram_id)
    async with sessions() as session:
        code = await rewards.invite_code(session, user_id)
        await session.commit()
        return code


async def _set(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        await session.merge(AppSetting(key=key.value, value=value))
        await session.commit()


async def join_through(chat: Chat, code: str) -> None:
    """Start the bot through an invite link, then complete the profile."""
    assert await chat.send(f"/start {rewards.INVITE_PREFIX}{code}") == TERMS
    await complete_onboarding(chat)


@pytest.fixture
async def alice(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    return chat


def chat_for(settings: BotSettings, redis: Redis, sessions: Sessions, telegram_id: int) -> Chat:
    return Chat(settings, redis, sessions, user_id=telegram_id)


# --- Welcome bonus ---------------------------------------------------------------------


async def test_completing_the_profile_grants_the_welcome_bonus(
    alice: Chat, session_factory: Sessions
) -> None:
    texts = alice.session.sent_texts()
    assert texts[-2:] == [ob.WELCOME_COINS.format(amount=10), ob.ONBOARDING_COMPLETE]
    assert await _total(session_factory, USER_ID) == 10


async def test_the_welcome_bonus_is_given_once_per_account_ever(
    alice: Chat, session_factory: Sessions
) -> None:
    alice_id = await _user_id(session_factory, USER_ID)
    async with session_factory() as session:
        await users.delete_account(session, alice_id, NOW)
        await session.commit()
    await complete_onboarding(alice)  # back after deleting the account
    assert await _total(session_factory, USER_ID) == 10  # kept, and not given again
    assert ob.WELCOME_COINS.format(amount=10) not in alice.session.sent_texts()[-2:]


async def test_the_welcome_bonus_can_be_turned_off(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    await _set(session_factory, SettingKey.WELCOME_BONUS_COINS, 0)
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    assert await _total(session_factory, USER_ID) == 0
    assert chat.session.sent_texts()[-1] == ob.ONBOARDING_COMPLETE


# --- Invites ---------------------------------------------------------------------------


async def test_an_invite_rewards_the_inviter_once_the_profile_is_complete(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    bob = chat_for(bot_settings, redis, session_factory, BOB_ID)
    code = await _code(session_factory, USER_ID)
    assert await bob.send(f"/start {rewards.INVITE_PREFIX}{code}") == TERMS
    # Alice is told someone joined, but not who; no coins yet.
    assert text_of(bob.session.sent_to(USER_ID)[-1]) == ob.INVITE_JOINED_REWARD.format(
        amount=10, cap=10
    )
    assert await _total(session_factory, USER_ID) == 10  # just her welcome bonus
    await complete_onboarding(bob)
    assert text_of(bob.session.sent_to(USER_ID)[-1]) == ob.INVITE_REWARDED.format(amount=10)
    assert await _total(session_factory, USER_ID) == 20
    assert await _total(session_factory, BOB_ID) == 10  # his welcome bonus only, never both


async def test_existing_and_deleted_accounts_cannot_be_invited(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    code = await _code(session_factory, USER_ID)
    bob = chat_for(bot_settings, redis, session_factory, BOB_ID)
    await complete_onboarding(bob)  # already has a profile
    await bob.send(f"/start {rewards.INVITE_PREFIX}{code}")
    carol = chat_for(bot_settings, redis, session_factory, CAROL_ID)
    await complete_onboarding(carol)
    carol_id = await _user_id(session_factory, CAROL_ID)
    async with session_factory() as session:
        await users.delete_account(session, carol_id, NOW)  # deleted, then comes back
        await session.commit()
    await join_through(carol, code)
    async with session_factory() as session:
        assert (await session.execute(select(Referral))).all() == []
    assert await _total(session_factory, USER_ID) == 10


async def test_someone_who_only_peeked_before_can_still_be_invited(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    bob = chat_for(bot_settings, redis, session_factory, BOB_ID)
    assert await bob.send("/start") == TERMS  # opened the bot once, never finished
    await join_through(bob, await _code(session_factory, USER_ID))
    assert await _total(session_factory, USER_ID) == 20


@pytest.mark.parametrize("payload", ["inv_nope", "hello", "inv_"])
async def test_unknown_payloads_are_ignored(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions, payload: str
) -> None:
    bob = chat_for(bot_settings, redis, session_factory, BOB_ID)
    assert await bob.send(f"/start {payload}") == TERMS
    async with session_factory() as session:
        assert (await session.execute(select(Referral))).all() == []


async def test_a_user_cannot_invite_themselves(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    code = await _code(session_factory, USER_ID)  # before finishing their own profile
    await join_through(chat, code)
    assert await _total(session_factory, USER_ID) == 10  # only the welcome bonus


async def test_the_first_inviter_stays(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    carol = chat_for(bot_settings, redis, session_factory, CAROL_ID)
    await complete_onboarding(carol)
    bob = chat_for(bot_settings, redis, session_factory, BOB_ID)
    await bob.send(f"/start {rewards.INVITE_PREFIX}{await _code(session_factory, USER_ID)}")
    await join_through(bob, await _code(session_factory, CAROL_ID))
    assert await _total(session_factory, USER_ID) == 20
    assert await _total(session_factory, CAROL_ID) == 10


async def test_invite_rewards_stop_at_the_daily_cap(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    await _set(session_factory, SettingKey.INVITE_REWARDS_PER_DAY, 1)
    code = await _code(session_factory, USER_ID)
    for telegram_id in (BOB_ID, CAROL_ID):
        await join_through(chat_for(bot_settings, redis, session_factory, telegram_id), code)
    assert await _total(session_factory, USER_ID) == 20  # one reward, not two
    async with session_factory() as session:
        statuses = sorted((await session.execute(select(Referral.status))).scalars())
        assert statuses == ["not_rewarded", "rewarded"]


async def test_restricted_inviters_are_not_rewarded(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    code = await _code(session_factory, USER_ID)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status="suspended")
        )
        await session.commit()
    await join_through(chat_for(bot_settings, redis, session_factory, BOB_ID), code)
    assert await _total(session_factory, USER_ID) == 10


async def test_invite_rewards_can_be_turned_off(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    await _set(session_factory, SettingKey.INVITE_REWARD_COINS, 0)
    bob = chat_for(bot_settings, redis, session_factory, BOB_ID)
    await bob.send(f"/start {rewards.INVITE_PREFIX}{await _code(session_factory, USER_ID)}")
    assert text_of(bob.session.sent_to(USER_ID)[-1]) == ob.INVITE_JOINED  # no coins promised
    await complete_onboarding(bob)
    assert await _total(session_factory, USER_ID) == 10


async def test_the_invite_code_is_stable_and_unguessable(session_factory: Sessions) -> None:
    first = await _code(session_factory, USER_ID)
    assert await _code(session_factory, USER_ID) == first
    assert len(first) == 8 and first != str(await _user_id(session_factory, USER_ID))
    assert await _code(session_factory, BOB_ID) != first  # each user has their own


# --- Concurrency -----------------------------------------------------------------------


async def _complete(sessions: Sessions, user_id: int, per_day: int = 10) -> rewards.Granted:
    async with sessions() as session:
        granted = await rewards.on_profile_complete(
            session, user_id, NOW, welcome=10, invite_reward=10, invites_per_day=per_day
        )
        await session.commit()
        return granted


async def _invite(sessions: Sessions, inviter: int, invitee_telegram_id: int) -> int:
    invitee = await _user_id(sessions, invitee_telegram_id)
    async with sessions() as session:
        code = await rewards.invite_code(session, inviter)
        assert await rewards.attach_invite(session, invitee, rewards.INVITE_PREFIX + code)
        await session.commit()
    return invitee


async def test_completing_twice_at_once_rewards_once(session_factory: Sessions) -> None:
    alice = await _user_id(session_factory, USER_ID)
    bob = await _invite(session_factory, alice, BOB_ID)
    results = await asyncio.gather(_complete(session_factory, bob), _complete(session_factory, bob))
    assert sorted(r.welcome for r in results) == [0, 10]
    assert sorted(r.invite_reward for r in results) == [0, 10]
    assert await _total(session_factory, BOB_ID) == 10
    assert await _total(session_factory, USER_ID) == 10


async def test_two_invitees_at_once_cannot_pass_the_cap(session_factory: Sessions) -> None:
    alice = await _user_id(session_factory, USER_ID)
    bob = await _invite(session_factory, alice, BOB_ID)
    carol = await _invite(session_factory, alice, CAROL_ID)
    async with session_factory() as first:
        granted = await rewards.on_profile_complete(
            first, bob, NOW, welcome=10, invite_reward=10, invites_per_day=1
        )
        assert granted.invite_reward == 10  # holds Alice's row until it commits
        second = asyncio.create_task(_complete(session_factory, carol, per_day=1))
        await wait_until_blocked(session_factory)  # really at the same time
        await first.commit()
        assert (await second).invite_reward == 0  # counted after Bob's, so over the cap
    assert await _total(session_factory, USER_ID) == 10


# --- Review follow-ups -----------------------------------------------------------------


async def test_the_daily_cap_starts_again_each_day(session_factory: Sessions) -> None:
    alice = await _user_id(session_factory, USER_ID)
    bob = await _invite(session_factory, alice, BOB_ID)
    carol = await _invite(session_factory, alice, CAROL_ID)
    async with session_factory() as session:  # Bob finished yesterday, at the cap of 1
        await rewards.on_profile_complete(
            session, bob, NOW - timedelta(days=1), welcome=10, invite_reward=10, invites_per_day=1
        )
        await session.commit()
    assert (await _complete(session_factory, carol, per_day=1)).invite_reward == 10
    assert await _total(session_factory, USER_ID) == 20


async def test_an_invitee_who_deleted_their_account_earns_nothing(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    bob = chat_for(bot_settings, redis, session_factory, BOB_ID)
    await bob.send(f"/start {rewards.INVITE_PREFIX}{await _code(session_factory, USER_ID)}")
    bob_id = await _user_id(session_factory, BOB_ID)
    async with session_factory() as session:  # deleted before finishing, then back
        await users.delete_account(session, bob_id, NOW)
        await session.commit()
    await complete_onboarding(bob)
    assert await _total(session_factory, USER_ID) == 10  # Alice's welcome bonus only
    async with session_factory() as session:
        assert (await session.execute(select(Referral.status))).scalar_one() == "not_rewarded"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE referrals SET status = 'pending', settled_at = NULL",
        "UPDATE referrals SET settled_at = now() - interval '1 day'",
        "DELETE FROM referrals",
    ],
)
async def test_a_settled_referral_cannot_be_rewritten(
    session_factory: Sessions, statement: str
) -> None:
    alice = await _user_id(session_factory, USER_ID)
    bob = await _invite(session_factory, alice, BOB_ID)
    await _complete(session_factory, bob)
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="referral") as raised:
            await session.execute(text(statement))
    assert raised.value.orig.sqlstate == "23001"  # type: ignore[union-attr]


async def test_an_invite_waits_for_an_account_deletion_in_progress(
    session_factory: Sessions,
) -> None:
    alice = await _user_id(session_factory, USER_ID)
    bob = await _user_id(session_factory, BOB_ID)
    async with session_factory() as session:
        code = await rewards.invite_code(session, alice)
        await session.commit()
    async with session_factory() as deleting:
        await users.lock_user(deleting, bob)  # as account deletion does first
        await users.delete_account(deleting, bob, NOW)

        async def attach() -> int | None:
            async with session_factory() as session:
                found = await rewards.attach_invite(session, bob, rewards.INVITE_PREFIX + code)
                await session.commit()
                return found

        task = asyncio.create_task(attach())
        await wait_until_blocked(session_factory)
        await deleting.commit()
        assert await task is None  # saw the deletion, so recorded nothing
