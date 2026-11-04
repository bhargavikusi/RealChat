"""The coin ledger, holds, admin adjustments, and /coins (D-050)."""

import asyncio
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps import admin_cli
from apps.bot import coins as bot_coins
from apps.bot.menu import BOT_COMMANDS
from backend.config.settings import BotSettings
from backend.modules.admin import service as admin
from backend.modules.admin.models import AdminAuditEntry
from backend.modules.admin.service import AdminError
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.coins.service import InsufficientCoinsError
from backend.modules.users import service as users
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked

Sessions = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
async def user_id(session_factory: Sessions) -> int:
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await session.commit()
        return user.id


async def _credit(sessions: Sessions, user_id: int, amount: int, key: str) -> bool:
    async with sessions() as session:
        added = await coins.credit(session, user_id, amount, "admin_grant", key)
        await session.commit()
        return added


async def _balance(sessions: Sessions, user_id: int) -> coins.Balance:
    async with sessions() as session:
        return await coins.balance(session, user_id)


# --- The ledger ------------------------------------------------------------------------


async def test_a_balance_is_the_sum_of_the_ledger(session_factory: Sessions, user_id: int) -> None:
    assert await _balance(session_factory, user_id) == coins.Balance(0, 0)
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        assert await coins.debit(session, user_id, 3, "paid_match", "b")
        await session.commit()
    assert (await _balance(session_factory, user_id)).total == 7


async def test_the_same_key_records_once(session_factory: Sessions, user_id: int) -> None:
    assert await _credit(session_factory, user_id, 10, "same")
    assert not await _credit(session_factory, user_id, 10, "same")
    async with session_factory() as session:
        assert await coins.debit(session, user_id, 4, "paid_match", "spend")
        assert not await coins.debit(session, user_id, 4, "paid_match", "spend")
        await session.commit()
    assert (await _balance(session_factory, user_id)).total == 6


async def test_balances_never_go_negative(session_factory: Sessions, user_id: int) -> None:
    await _credit(session_factory, user_id, 2, "a")
    async with session_factory() as session:
        with pytest.raises(InsufficientCoinsError) as raised:
            await coins.debit(session, user_id, 3, "paid_match", "b")
        assert (raised.value.available, raised.value.needed) == (2, 3)


@pytest.mark.parametrize("amount", [0, -1])
async def test_credits_and_debits_must_be_positive(
    session_factory: Sessions, user_id: int, amount: int
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await coins.credit(session, user_id, amount, "admin_grant", "x")
        with pytest.raises(ValueError):
            await coins.debit(session, user_id, amount, "paid_match", "y")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE coin_transactions SET amount = 1000",
        "DELETE FROM coin_transactions",
        "TRUNCATE coin_transactions",
    ],
)
async def test_the_ledger_is_append_only(
    session_factory: Sessions, user_id: int, statement: str
) -> None:
    await _credit(session_factory, user_id, 5, "a")
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(statement))


async def test_history_is_newest_first(session_factory: Sessions, user_id: int) -> None:
    for i in range(3):
        await _credit(session_factory, user_id, i + 1, f"k{i}")
    async with session_factory() as session:
        assert [e.amount for e in await coins.history(session, user_id, 2)] == [3, 2]


# --- Holds -----------------------------------------------------------------------------


async def test_held_coins_are_not_available(session_factory: Sessions, user_id: int) -> None:
    await _credit(session_factory, user_id, 5, "a")
    async with session_factory() as session:
        await coins.hold(session, user_id, 4, "paid_match", "search-1")
        await session.commit()
    balance = await _balance(session_factory, user_id)
    assert (balance.total, balance.held, balance.available) == (5, 4, 1)
    async with session_factory() as session:
        with pytest.raises(InsufficientCoinsError):
            await coins.hold(session, user_id, 2, "paid_match", "search-2")
        with pytest.raises(InsufficientCoinsError):
            await coins.debit(session, user_id, 2, "paid_match", "spend")


async def test_a_hold_is_captured_or_released_once(session_factory: Sessions, user_id: int) -> None:
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        first = await coins.hold(session, user_id, 2, "paid_match", "search-1")
        assert await coins.hold(session, user_id, 2, "paid_match", "search-1") == first
        second = await coins.hold(session, user_id, 2, "paid_match", "search-2")
        assert await coins.capture(session, first, "paid_match", NOW)
        assert not await coins.capture(session, first, "paid_match", NOW)
        assert not await coins.release(session, first, NOW)
        assert await coins.release(session, second, NOW)
        assert not await coins.capture(session, second, "paid_match", NOW)
        await session.commit()
    assert await _balance(session_factory, user_id) == coins.Balance(8, 0)
    async with session_factory() as session:
        spent = (
            await session.execute(select(CoinTransaction).where(CoinTransaction.amount < 0))
        ).scalar_one()
        assert (spent.amount, spent.kind, spent.hold_id) == (-2, "paid_match", first)


# --- Concurrency -----------------------------------------------------------------------


@pytest.mark.parametrize("spend", ["debit", "hold"])
async def test_simultaneous_spends_cannot_overdraw(
    session_factory: Sessions, user_id: int, spend: str
) -> None:
    await _credit(session_factory, user_id, 10, "a")

    async def spend_six(session: AsyncSession, key: str) -> None:
        if spend == "debit":
            await coins.debit(session, user_id, 6, "paid_match", key)
        else:
            await coins.hold(session, user_id, 6, "paid_match", key)

    async with session_factory() as first:
        await spend_six(first, "first")

        async def second() -> bool:
            async with session_factory() as session:
                try:
                    await spend_six(session, "second")
                except InsufficientCoinsError:
                    return False
                await session.commit()
                return True

        task = asyncio.create_task(second())
        await wait_until_blocked(session_factory)
        assert not task.done()  # waits for the first spend's lock on the user
        await first.commit()
        assert await task is False
    assert (await _balance(session_factory, user_id)).available == 4


async def test_simultaneous_captures_spend_once(session_factory: Sessions, user_id: int) -> None:
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        hold_id = await coins.hold(session, user_id, 2, "paid_match", "search")
        await session.commit()

    async def capture() -> bool:
        async with session_factory() as session:
            done = await coins.capture(session, hold_id, "paid_match", NOW)
            await session.commit()
            return done

    assert sorted(await asyncio.gather(capture(), capture())) == [False, True]
    assert (await _balance(session_factory, user_id)).total == 8


# --- Admin adjustments -----------------------------------------------------------------


async def test_admins_grant_and_correct_with_a_reason(
    session_factory: Sessions, user_id: int
) -> None:
    async with session_factory() as session:
        assert (await admin.adjust_coins(session, USER_ID, 10, "launch promo")).total == 10
        assert (await admin.adjust_coins(session, USER_ID, -4, "double grant")).total == 6
        with pytest.raises(AdminError, match="can't go below 0"):
            await admin.adjust_coins(session, USER_ID, -7, "too much")
        with pytest.raises(AdminError, match="reason"):
            await admin.adjust_coins(session, USER_ID, 5, "  ")
        with pytest.raises(AdminError, match="must not be 0"):
            await admin.adjust_coins(session, USER_ID, 0, "nothing")
        with pytest.raises(AdminError, match="start the bot first"):
            await admin.adjust_coins(session, 999_999, 5, "nobody")
        await session.commit()
    async with session_factory() as session:
        entries = (
            await session.execute(select(CoinTransaction).order_by(CoinTransaction.id))
        ).scalars()
        assert [(e.kind, e.amount, e.note) for e in entries] == [
            ("admin_grant", 10, "launch promo"),
            ("admin_correction", -4, "double grant"),
        ]
        audit = (await session.execute(select(AdminAuditEntry))).scalars().all()
        assert [(a.action, a.target_user_id, a.new_value) for a in audit] == [
            ("coins_adjusted", user_id, "10"),
            ("coins_adjusted", user_id, "-4"),
        ]


async def test_the_cli_adjusts_and_shows_coins(
    session_factory: Sessions, user_id: int, capsys: pytest.CaptureFixture[str]
) -> None:
    run = admin_cli.run
    assert await run(["coins", str(USER_ID), "5", "--reason", "support"], session_factory) == 0
    assert await run(["balance", str(USER_ID)], session_factory) == 0
    assert await run(["coins", str(USER_ID), "-9", "--reason", "oops"], session_factory) == 1
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        f"{USER_ID}: 5 coins (0 held, 5 available)",
        f"{USER_ID}: 5 coins (0 held, 5 available)",
    ]
    assert "can't go below 0" in output.err
    with pytest.raises(SystemExit):
        await run(["coins", str(USER_ID), "5"], session_factory)  # a reason is required


async def test_coins_survive_account_deletion(session_factory: Sessions, user_id: int) -> None:
    await _credit(session_factory, user_id, 7, "a")
    async with session_factory() as session:
        await users.delete_account(session, user_id, NOW)
        await session.commit()
    assert (await _balance(session_factory, user_id)).total == 7


# --- /coins ----------------------------------------------------------------------------


async def test_coins_is_in_the_command_menu() -> None:
    assert "coins" in [command.command for command in BOT_COMMANDS]


async def test_coins_needs_onboarding_first(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    assert await chat.send("/coins") == TERMS


async def test_coins_shows_balance_held_coins_and_history(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    assert await chat.send("/coins") == "\n".join(
        ["💰 Your coins", "", "Balance: 0", "", "Recent activity:", bot_coins.NO_ACTIVITY]
    )
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await admin.adjust_coins(session, USER_ID, 10, "promo")
        await coins.hold(session, user.id, 2, "paid_match", "search")
        await session.commit()
    shown = await chat.send("/coins")
    today = datetime.now(UTC).strftime("%d %b")
    assert shown.splitlines() == [
        "💰 Your coins",
        "",
        "Balance: 10",
        "Held for a search: 2",
        "Available: 8",
        "",
        "Recent activity:",
        f"+10  Bonus from RealChat · {today}",
    ]


async def test_a_repeated_spend_reports_done_even_when_coins_ran_out(
    session_factory: Sessions, user_id: int
) -> None:
    await _credit(session_factory, user_id, 4, "a")
    async with session_factory() as session:
        assert await coins.debit(session, user_id, 4, "paid_match", "spend")
        await session.commit()
    async with session_factory() as session:  # e.g. a retry after a lost reply
        assert not await coins.debit(session, user_id, 4, "paid_match", "spend")
