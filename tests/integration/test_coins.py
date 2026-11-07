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
from backend.modules.admin.models import AdminAuditEntry, AppSetting
from backend.modules.admin.service import AdminError
from backend.modules.admin.settings import SettingKey
from backend.modules.coins import rewards
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.coins.service import InsufficientCoinsError
from backend.modules.users import service as users
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked

Sessions = async_sessionmaker[AsyncSession]
# PostgreSQL error codes, so a test cannot pass on the wrong database failure.
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
RESTRICT_VIOLATION = "23001"


def sqlstate(raised: pytest.ExceptionInfo[DBAPIError]) -> str:
    return str(raised.value.orig.sqlstate)  # type: ignore[union-attr]


NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
async def user_id(session_factory: Sessions) -> int:
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await session.commit()
        return user.id


async def _credit(sessions: Sessions, user_id: int, amount: int, key: str) -> bool:
    async with sessions() as session:
        added = await coins.credit(session, user_id, amount, "admin_grant", key, note="test")
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
            await coins.credit(session, user_id, amount, "admin_grant", "x", note="test")
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
        with pytest.raises(DBAPIError, match="append-only") as raised:
            await session.execute(text(statement))
    # restrict_violation: the trigger, not some other error.
    assert sqlstate(raised) == RESTRICT_VIOLATION


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
        assert await coins.capture(session, first, NOW)
        assert not await coins.capture(session, first, NOW)
        assert not await coins.release(session, first, NOW)
        assert await coins.release(session, second, NOW)
        assert not await coins.capture(session, second, NOW)
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
    async with session_factory() as first:
        assert await coins.capture(first, hold_id, NOW)  # holds the hold's row lock

        async def second() -> bool:
            async with session_factory() as session:
                done = await coins.capture(session, hold_id, NOW)
                await session.commit()
                return done

        task = asyncio.create_task(second())
        await wait_until_blocked(session_factory)  # really overlapping
        await first.commit()
        assert await task is False
    assert (await _balance(session_factory, user_id)).total == 8


# --- Admin adjustments -----------------------------------------------------------------


async def test_admins_grant_and_correct_with_a_reason(
    session_factory: Sessions, user_id: int
) -> None:
    async with session_factory() as session:
        assert (await admin.adjust_coins(session, USER_ID, 10, "launch promo"))[0].total == 10
        assert (await admin.adjust_coins(session, USER_ID, -4, "double grant"))[0].total == 6
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


async def test_coins_are_forfeited_on_account_deletion(
    session_factory: Sessions, user_id: int
) -> None:
    await _credit(session_factory, user_id, 7, "a")
    async with session_factory() as session:
        await users.delete_account(session, user_id, NOW)
        await session.commit()
    # Removed by a new entry; the original credit is still in the ledger (D-055).
    assert (await _balance(session_factory, user_id)).total == 0


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
    await complete_onboarding(chat)  # with the 10-coin welcome bonus
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await admin.adjust_coins(session, USER_ID, 5, "promo")
        await coins.hold(session, user.id, 2, "paid_match", "search")
        code = await rewards.invite_code(session, user.id)
        await session.commit()
    shown = await chat.send("/coins")
    today = datetime.now(UTC).strftime("%d %b")
    assert shown.splitlines() == [
        "💰 Your coins",
        "",
        "Balance: 15",
        "Held for a search: 2",
        "Available: 13",
        "",
        "Recent activity:",
        f"+5  Bonus from RealChat · {today}",
        f"+10  Welcome bonus · {today}",
        "",
        f"👥 Invite friends: https://t.me/RealChatTestBot?start=inv_{code}",
        bot_coins.INVITE_REWARD.format(amount=10, cap=10),
    ]


async def test_coins_says_when_there_is_no_activity(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.WELCOME_BONUS_COINS.value, value=0))
        session.add(AppSetting(key=SettingKey.INVITE_REWARD_COINS.value, value=0))
        await session.commit()
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    lines = (await chat.send("/coins")).splitlines()
    assert lines[:6] == [
        "💰 Your coins",
        "",
        "Balance: 0",
        "",
        "Recent activity:",
        bot_coins.NO_ACTIVITY,
    ]
    assert lines[7].startswith("👥 Invite friends: ")
    assert len(lines) == 8  # no reward line while invite rewards are off


async def test_a_repeated_spend_reports_done_even_when_coins_ran_out(
    session_factory: Sessions, user_id: int
) -> None:
    await _credit(session_factory, user_id, 4, "a")
    async with session_factory() as session:
        assert await coins.debit(session, user_id, 4, "paid_match", "spend")
        await session.commit()
    async with session_factory() as session:  # e.g. a retry after a lost reply
        assert not await coins.debit(session, user_id, 4, "paid_match", "spend")


# --- Review follow-ups -----------------------------------------------------------------


async def test_a_hold_key_cannot_be_reused_for_another_hold(
    session_factory: Sessions, user_id: int
) -> None:
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        other = await users.get_or_create_user(session, 5353)
        await coins.credit(session, other.id, 10, "admin_grant", "b", note="test")
        await coins.hold(session, user_id, 2, "paid_match", "search")
        with pytest.raises(ValueError, match="different hold"):
            await coins.hold(session, other.id, 2, "paid_match", "search")  # another user
        with pytest.raises(ValueError, match="different hold"):
            await coins.hold(session, user_id, 3, "paid_match", "search")  # another amount


_INSERT = "INSERT INTO coin_transactions (user_id, amount, kind, idempotency_key, hold_id, note) "


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (_INSERT + "VALUES (:u, 5, 'paid_match', 'k1', NULL, NULL)", "ck_coin_transactions_sign"),
        (
            _INSERT + "VALUES (:u, -5, 'welcome_bonus', 'k2', NULL, NULL)",
            "ck_coin_transactions_sign",
        ),
        (
            _INSERT + "VALUES (:u, 5, 'admin_grant', 'k3', NULL, NULL)",
            "ck_coin_transactions_admin_note",
        ),
        # The hold trigger runs before the CHECK, and refuses it first.
        (
            _INSERT + "VALUES (:u, 5, 'welcome_bonus', 'k4', :h, NULL)",
            "ck_coin_transactions_hold_kind|does not match its coin hold",
        ),
    ],
)
async def test_the_database_enforces_ledger_rules(
    session_factory: Sessions, user_id: int, statement: str, constraint: str
) -> None:
    await _credit(session_factory, user_id, 5, "funds")
    async with session_factory() as session:
        hold_id = await coins.hold(session, user_id, 1, "paid_match", "h")
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match=constraint) as raised:
            await session.execute(text(statement), {"u": user_id, "h": hold_id})
    assert sqlstate(raised) in (CHECK_VIOLATION, RESTRICT_VIOLATION)


async def test_the_database_settles_a_hold_once(session_factory: Sessions, user_id: int) -> None:
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        hold_id = await coins.hold(session, user_id, 2, "paid_match", "search")
        assert await coins.capture(session, hold_id, NOW)
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="only go from held") as raised:
            await session.execute(
                text("UPDATE coin_holds SET status = 'held', settled_at = NULL WHERE id = :h"),
                {"h": hold_id},
            )
    assert sqlstate(raised) == RESTRICT_VIOLATION
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="uq_coin_transactions_hold_id") as raised:
            await session.execute(
                text(
                    "INSERT INTO coin_transactions (user_id, amount, kind, idempotency_key, "
                    "hold_id) VALUES (:u, -2, 'paid_match', 'again', :h)"
                ),
                {"u": user_id, "h": hold_id},
            )
    assert sqlstate(raised) == UNIQUE_VIOLATION


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM coin_holds WHERE id = :h",
        "UPDATE coin_holds SET amount = 1 WHERE id = :h",
        "UPDATE coin_holds SET user_id = :o WHERE id = :h",
        "UPDATE coin_holds SET status = 'captured', settled_at = now(), amount = 1 WHERE id = :h",
    ],
)
async def test_a_held_hold_cannot_be_deleted_or_altered(
    session_factory: Sessions, user_id: int, statement: str
) -> None:
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        other = await users.get_or_create_user(session, 5353)
        hold_id = await coins.hold(session, user_id, 2, "paid_match", "search")
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="hold") as raised:
            await session.execute(text(statement), {"h": hold_id, "o": other.id})
    assert sqlstate(raised) == RESTRICT_VIOLATION


@pytest.mark.parametrize(
    ("amount", "owner", "captured"),
    [(-2, "same", False), (-2, "other", True), (-1, "same", True)],
    ids=["hold-not-captured", "another-users-hold", "wrong-amount"],
)
async def test_a_hold_entry_must_match_its_hold(
    session_factory: Sessions, user_id: int, amount: int, owner: str, captured: bool
) -> None:
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        other = await users.get_or_create_user(session, 5353)
        hold_id = await coins.hold(session, user_id, 2, "paid_match", "search")
        await session.commit()
    entry_user = user_id if owner == "same" else other.id
    async with session_factory() as session:
        if captured:
            # Captured in the same transaction as the bad entry (a captured hold must
            # have its entry by commit).
            await session.execute(
                text("UPDATE coin_holds SET status = 'captured', settled_at = now() WHERE id = :h"),
                {"h": hold_id},
            )
        with pytest.raises(DBAPIError, match="does not match its coin hold") as raised:
            await session.execute(
                text(
                    "INSERT INTO coin_transactions (user_id, amount, kind, idempotency_key, "
                    "hold_id) VALUES (:u, :a, 'paid_match', 'entry', :h)"
                ),
                {"u": entry_user, "a": amount, "h": hold_id},
            )
    assert sqlstate(raised) == RESTRICT_VIOLATION


async def test_a_captured_hold_must_have_its_entry(session_factory: Sessions, user_id: int) -> None:
    await _credit(session_factory, user_id, 10, "a")
    async with session_factory() as session:
        hold_id = await coins.hold(session, user_id, 2, "paid_match", "search")
        await session.commit()
    async with session_factory() as session:
        await session.execute(
            text("UPDATE coin_holds SET status = 'captured', settled_at = now() WHERE id = :h"),
            {"h": hold_id},
        )
        with pytest.raises(DBAPIError, match="has no ledger entry") as raised:
            await session.commit()  # checked at commit
    assert sqlstate(raised) == RESTRICT_VIOLATION
    assert (await _balance(session_factory, user_id)) == coins.Balance(10, 2)  # still held


async def test_a_hold_must_start_as_held(session_factory: Sessions, user_id: int) -> None:
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="must start as held") as raised:
            await session.execute(
                text(
                    "INSERT INTO coin_holds (user_id, amount, purpose, idempotency_key, status, "
                    "settled_at) VALUES (:u, 2, 'paid_match', 'k', 'captured', now())"
                ),
                {"u": user_id},
            )
    assert sqlstate(raised) == RESTRICT_VIOLATION


async def test_an_admin_key_makes_a_rerun_harmless(
    session_factory: Sessions, user_id: int, capsys: pytest.CaptureFixture[str]
) -> None:
    command = ["coins", str(USER_ID), "5", "--reason", "promo", "--key", "promo-1"]
    assert await admin_cli.run(command, session_factory) == 0
    assert await admin_cli.run(command, session_factory) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{USER_ID}: 5 coins (0 held, 5 available)",
        "That key was already used, so nothing changed.",
        f"{USER_ID}: 5 coins (0 held, 5 available)",
    ]
    async with session_factory() as session:
        audit = (await session.execute(select(AdminAuditEntry))).scalars().all()
        assert len(audit) == 1


@pytest.mark.parametrize("amount", [2_147_483_648, -2_147_483_648])
async def test_admin_amounts_must_fit(session_factory: Sessions, user_id: int, amount: int) -> None:
    async with session_factory() as session:
        with pytest.raises(AdminError, match="between"):
            await admin.adjust_coins(session, USER_ID, amount, "too much")


async def test_coins_abandons_a_pending_profile_edit(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    await chat.send("/profile")
    await chat.press("✏️ Nickname")  # now waiting for a new nickname
    await chat.send("/coins")
    await chat.send("Zed")
    async with session_factory() as session:
        profile = await users.get_profile(
            session, (await users.get_or_create_user(session, USER_ID)).id
        )
        assert profile.nickname == "Night Owl"


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (["coins", str(USER_ID), "6", "--reason", "promo", "--key", "k"], "different adjustment"),
        (["coins", str(USER_ID), "5", "--reason", "other", "--key", "k"], "different adjustment"),
        (["coins", "5353", "5", "--reason", "promo", "--key", "k"], "different adjustment"),
        (["coins", str(USER_ID), "5", "--reason", "promo", "--key", "  "], "1 to 122"),
        (["coins", str(USER_ID), "5", "--reason", "promo", "--key", "x" * 123], "1 to 122"),
    ],
)
async def test_an_admin_key_is_for_one_adjustment(
    session_factory: Sessions,
    user_id: int,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
    error: str,
) -> None:
    async with session_factory() as session:
        await users.get_or_create_user(session, 5353)
        await session.commit()
    first = ["coins", str(USER_ID), "5", "--reason", "promo", "--key", "k"]
    assert await admin_cli.run(first, session_factory) == 0
    capsys.readouterr()
    assert await admin_cli.run(command, session_factory) == 1
    assert error in capsys.readouterr().err
    assert (await _balance(session_factory, user_id)).total == 5
