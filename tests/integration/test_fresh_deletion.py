"""Account deletion starts completely fresh (D-055): coins are forfeited through the
ledger, the invite code is retired, and pending invites are settled unrewarded."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import coins as bot_coins
from backend.config.settings import BotSettings
from backend.modules.coins import rewards
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinHold, CoinTransaction, InviteCode, Referral
from backend.modules.users import service as users
from backend.modules.users.models import User
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked

Sessions = async_sessionmaker[AsyncSession]
BOB_ID = 5353
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


async def user_id_of(sessions: Sessions, telegram_id: int = USER_ID) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        await session.commit()
        return user.id


async def ledger(sessions: Sessions, user_id: int) -> list[tuple[int, str, str, datetime]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                CoinTransaction.amount,
                CoinTransaction.kind,
                CoinTransaction.idempotency_key,
                CoinTransaction.created_at,
            )
            .where(CoinTransaction.user_id == user_id)
            .order_by(CoinTransaction.id)
        )
        return [(int(a), k, key, at) for a, k, key, at in rows]


async def total(sessions: Sessions, user_id: int) -> int:
    async with sessions() as session:
        return (await coins.balance(session, user_id)).total


async def credit(sessions: Sessions, user_id: int, amount: int, key: str) -> None:
    async with sessions() as session:
        await coins.credit(session, user_id, amount, "admin_grant", key, note="test")
        await session.commit()


async def delete(sessions: Sessions, user_id: int, now: datetime = NOW) -> None:
    async with sessions() as session:
        await users.delete_account(session, user_id, now)
        await session.commit()


@pytest.fixture
async def alice(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    """Onboarded, with the 10-coin welcome bonus."""
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    return chat


# --- The warning -----------------------------------------------------------------------


async def test_the_warning_lists_what_is_lost_with_the_balance(
    alice: Chat, session_factory: Sessions
) -> None:
    await credit(session_factory, await user_id_of(session_factory), 15, "extra")
    await alice.send("/profile")
    await alice.press(ac.ADVANCED_SETTINGS)
    warning = await alice.press(ac.DELETE_BUTTON)
    assert warning == ac.DELETE_CONFIRM.format(coins="25 coins")
    assert "• Your coins (you have 25 coins)" in warning
    # Cancelling changes nothing.
    assert await alice.press(ac.KEEP_BUTTON) == ac.KEPT
    assert await total(session_factory, await user_id_of(session_factory)) == 25


# --- Coins -----------------------------------------------------------------------------


async def test_deletion_forfeits_the_balance_and_keeps_the_history(
    alice: Chat, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    await credit(session_factory, user_id, 15, "extra")
    before = await ledger(session_factory, user_id)
    await delete(session_factory, user_id)
    after = await ledger(session_factory, user_id)
    assert after[: len(before)] == before  # nothing earlier is changed
    assert after[len(before) :] == [
        (-25, "account_deleted", f"deleted:{user_id}:{NOW.isoformat()}", NOW)
    ]
    assert await total(session_factory, user_id) == 0


async def test_nothing_is_recorded_for_an_empty_balance(session_factory: Sessions) -> None:
    user_id = await user_id_of(session_factory)
    await delete(session_factory, user_id)
    assert await ledger(session_factory, user_id) == []


async def test_held_coins_are_released_then_forfeited(
    alice: Chat, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    async with session_factory() as session:
        hold_id = await coins.hold(session, user_id, 4, "paid_match", "search:test")
        await session.commit()
    await delete(session_factory, user_id)
    async with session_factory() as session:
        assert await coins.hold_status(session, hold_id) == "released"
        balance = await coins.balance(session, user_id)
    assert (balance.total, balance.held) == (0, 0)
    assert (await ledger(session_factory, user_id))[-1][:2] == (-10, "account_deleted")


async def test_the_same_deletion_forfeits_once_and_a_later_one_again(
    alice: Chat, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    await delete(session_factory, user_id)
    async with session_factory() as session:
        # Repeated for the same deletion: already forfeited, nothing more.
        assert await coins.forfeit_all(session, user_id, NOW) == 0
        await session.commit()
    later = NOW + timedelta(days=3)
    await credit(session_factory, user_id, 6, "after-return")
    await delete(session_factory, user_id, later)
    kinds = [(a, k) for a, k, _, _ in await ledger(session_factory, user_id)]
    assert kinds.count((-10, "account_deleted")) == 1
    assert kinds[-1] == (-6, "account_deleted")
    assert await total(session_factory, user_id) == 0


async def test_a_spend_waiting_on_a_deletion_finds_no_coins(
    alice: Chat, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    async with session_factory() as deleting:
        await users.delete_account(deleting, user_id, NOW)

        async def spend() -> None:
            async with session_factory() as session:
                with pytest.raises(coins.InsufficientCoinsError):
                    await coins.debit(session, user_id, 5, "paid_match", "spend:race")

        task = asyncio.create_task(spend())
        await wait_until_blocked(session_factory)
        assert not task.done()
        await deleting.commit()
        await task
    assert await total(session_factory, user_id) == 0


async def test_a_daily_claim_racing_a_deletion_adds_nothing(
    alice: Chat, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    async with session_factory() as deleting:
        await users.delete_account(deleting, user_id, NOW)

        async def claim() -> bool:
            async with session_factory() as session:
                claimed = await rewards.claim_daily(session, user_id, NOW, 1)
                await session.commit()
                return claimed

        task = asyncio.create_task(claim())
        await wait_until_blocked(session_factory)
        await deleting.commit()
        assert not await task
    assert await total(session_factory, user_id) == 0


async def test_restricted_accounts_forfeit_their_coins_too(
    alice: Chat, session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status="suspended")
        )
        await session.commit()
    await alice.send("/profile")
    assert await alice.press(ac.DELETE_BUTTON) == ac.DELETE_CONFIRM.format(coins="10 coins")
    assert await alice.press(ac.CONFIRM_BUTTON) == ac.DELETED_RESTRICTED
    assert await total(session_factory, await user_id_of(session_factory)) == 0


async def test_a_returning_user_sees_only_new_activity(
    alice: Chat, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    await delete(session_factory, user_id, datetime.now(UTC))
    await complete_onboarding(alice)
    await credit(session_factory, user_id, 3, "back")
    reply = await alice.send("/coins")
    assert bot_coins.LABELS["admin_grant"] in reply
    assert bot_coins.LABELS["welcome_bonus"] not in reply
    assert bot_coins.LABELS["account_deleted"] not in reply
    assert await total(session_factory, user_id) == 3


# --- Invites ---------------------------------------------------------------------------


async def test_deletion_retires_the_invite_code(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    async with session_factory() as session:
        old = await rewards.invite_code(session, user_id)
        await session.commit()
    await delete(session_factory, user_id)
    async with session_factory() as session:
        assert (
            await session.scalar(select(InviteCode.code).where(InviteCode.user_id == user_id))
            is None
        )
        new = await rewards.invite_code(session, user_id)
        await session.commit()
    assert new != old
    # Someone following the old link credits nobody.
    bob = Chat(bot_settings, redis, session_factory, user_id=BOB_ID)
    assert await bob.send(f"/start {rewards.INVITE_PREFIX}{old}") == TERMS
    async with session_factory() as session:
        assert await session.scalar(select(Referral.invitee_user_id)) is None


async def test_pending_invites_are_not_rewarded_after_deletion(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    async with session_factory() as session:
        code = await rewards.invite_code(session, user_id)
        await session.commit()
    bob = Chat(bot_settings, redis, session_factory, user_id=BOB_ID)
    await bob.send(f"/start {rewards.INVITE_PREFIX}{code}")  # joined, not finished yet
    await delete(session_factory, user_id)
    await complete_onboarding(alice)  # Alice comes back
    await complete_onboarding(bob)  # and Bob finishes his profile afterwards
    async with session_factory() as session:
        status = await session.scalar(select(Referral.status))
    assert status == "not_rewarded"
    assert await total(session_factory, user_id) == 0


async def test_an_invite_waiting_on_the_inviters_deletion_is_not_recorded(
    alice: Chat, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    bob_id = await user_id_of(session_factory, BOB_ID)
    async with session_factory() as session:
        code = await rewards.invite_code(session, user_id)
        await session.commit()
    async with session_factory() as deleting:
        await users.delete_account(deleting, user_id, NOW)

        async def follow() -> int | None:
            async with session_factory() as session:
                found = await rewards.attach_invite(
                    session, bob_id, f"{rewards.INVITE_PREFIX}{code}"
                )
                await session.commit()
                return found

        task = asyncio.create_task(follow())
        await wait_until_blocked(session_factory)
        assert not task.done()
        await deleting.commit()
        assert await task is None


async def test_a_completion_waiting_on_the_inviters_deletion_is_not_rewarded(
    alice: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    user_id = await user_id_of(session_factory)
    async with session_factory() as session:
        code = await rewards.invite_code(session, user_id)
        await session.commit()
    bob = Chat(bot_settings, redis, session_factory, user_id=BOB_ID)
    await bob.send(f"/start {rewards.INVITE_PREFIX}{code}")
    bob_id = await user_id_of(session_factory, BOB_ID)
    async with session_factory() as deleting:
        await users.delete_account(deleting, user_id, NOW)

        async def complete() -> rewards.Granted:
            async with session_factory() as session:
                granted = await rewards.on_profile_complete(
                    session, bob_id, NOW, welcome=10, invite_reward=10, invites_per_day=10
                )
                await session.commit()
                return granted

        task = asyncio.create_task(complete())
        await wait_until_blocked(session_factory)
        assert not task.done()  # waits for the deletion, without deadlocking
        await deleting.commit()
        granted = await task
    assert granted.inviter_id is None
    assert await total(session_factory, user_id) == 0
    async with session_factory() as session:
        assert await session.scalar(select(Referral.status)) == "not_rewarded"


async def test_held_coins_in_the_database_cannot_be_left_held(
    alice: Chat, session_factory: Sessions
) -> None:
    """After deletion no hold of the account is still held."""
    user_id = await user_id_of(session_factory)
    async with session_factory() as session:
        await coins.hold(session, user_id, 2, "paid_match", "search:a")
        await coins.hold(session, user_id, 3, "paid_match", "search:b")
        await session.commit()
    await delete(session_factory, user_id)
    async with session_factory() as session:
        held = await session.scalar(
            select(CoinHold.id).where(CoinHold.user_id == user_id, CoinHold.status == "held")
        )
    assert held is None
