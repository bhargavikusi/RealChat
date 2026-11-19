"""Refunding Stars purchases: by an admin, or reported by Telegram (D-065)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import SendMessage
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps import admin_cli
from apps.bot import payments as bot_payments
from apps.bot import vip as bot_vip
from apps.bot.payments import vip_date
from backend.config.settings import BotSettings
from backend.modules.admin.models import AdminAuditEntry
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.payments import service as payments
from backend.modules.payments.models import StarsPayment
from backend.modules.users import service as users
from backend.modules.users.models import User
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked

Sessions = async_sessionmaker[AsyncSession]
PACK_1 = bot_payments.pack_label(payments.Pack(1, 150, 80))
BUY_VIP = bot_vip.BUY_BUTTON.format(stars=780)
EXTEND_VIP = bot_vip.EXTEND_BUTTON.format(stars=780)
PASS = timedelta(days=180)
SLACK = timedelta(seconds=30)


@pytest.fixture
async def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)  # with the 10-coin welcome bonus
    return chat


async def buy_pack(chat: Chat, charge_id: str) -> str:
    """Buy pack 1 (150 coins for 80 Stars); returns its payload."""
    await chat.send("/coins")
    await chat.press(PACK_1)
    payload = chat.session.invoices()[-1].payload
    await chat.checkout(payload, 80)
    await chat.paid(charge_id, payload, 80)
    return payload


async def buy_vip(chat: Chat, charge_id: str, button: str = BUY_VIP) -> str:
    """Buy a VIP pass (180 days and 3,750 coins for 780 Stars); returns its payload."""
    await chat.send("/vip")
    await chat.press(button)
    payload = chat.session.invoices()[-1].payload
    await chat.checkout(payload, 780)
    await chat.paid(charge_id, payload, 780)
    return payload


async def payment(sessions: Sessions, charge_id: str) -> StarsPayment:
    async with sessions() as session:
        return (
            await session.execute(
                select(StarsPayment).where(StarsPayment.telegram_charge_id == charge_id)
            )
        ).scalar_one()


async def account(sessions: Sessions) -> User:
    async with sessions() as session:
        return await users.get_or_create_user(session, USER_ID)


async def balance(sessions: Sessions) -> coins.Balance:
    async with sessions() as session:
        return await coins.balance(session, (await account(sessions)).id)


async def refund_entries(sessions: Sessions) -> list[tuple[int, str]]:
    async with sessions() as session:
        rows = await session.execute(
            select(CoinTransaction.amount, CoinTransaction.idempotency_key)
            .where(CoinTransaction.kind == "purchase_refund")
            .order_by(CoinTransaction.id)
        )
        return [(int(amount), key) for amount, key in rows]


async def spend(sessions: Sessions, amount: int, key: str = "spent") -> None:
    async with sessions() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await coins.debit(session, user.id, amount, "activity_alert", key)
        await session.commit()


async def cli(sessions: Sessions, *args: str) -> int:
    return await admin_cli.run(list(args), sessions)


async def admin_refund(sessions: Sessions, charge_id: str, reason: str = "support") -> int:
    found = await payment(sessions, charge_id)
    return await cli(sessions, "refund", str(found.id), "--reason", reason)


def notices(chat: Chat) -> list[str]:
    return [m.text for m in chat.session.requests if isinstance(m, SendMessage)]


def refund_notices(chat: Chat) -> list[str]:
    return [t for t in notices(chat) if t.startswith("💸")]


async def run_worker(chat: Chat, sessions: Sessions) -> int:
    retries = bot_payments.RefundRetries(chat.bot, sessions)
    return await retries.tick(datetime.now(UTC) + timedelta(seconds=1))


# --- Admin refunds ----------------------------------------------------------------------


async def test_an_admin_refund_takes_the_coins_back_and_the_worker_returns_the_stars(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    await buy_pack(chat, "charge-1")
    assert (await balance(session_factory)).total == 160
    assert await admin_refund(session_factory, "charge-1", "  bought by mistake ") == 0
    found = await payment(session_factory, "charge-1")
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"Purchase #{found.id}: 150 coins taken back. The worker will return the Stars."
    )
    assert (found.status, found.refund_reason, found.refund_note) == (
        "refund_due",
        "admin",
        "bought by mistake",
    )
    assert (await balance(session_factory)).total == 10
    assert await refund_entries(session_factory) == [(-150, f"refund:{found.id}")]
    async with session_factory() as session:
        audit = (await session.execute(select(AdminAuditEntry))).scalars().all()
    assert [(a.action, a.actor_user_id, a.new_value) for a in audit] == [
        ("purchase_refunded", None, str(found.id))
    ]
    # Nothing is said until Telegram has returned the Stars.
    assert refund_notices(chat) == []
    assert await run_worker(chat, session_factory) == 1
    [refund] = chat.session.refunds()
    assert (refund.user_id, refund.telegram_payment_charge_id) == (USER_ID, "charge-1")
    assert (await payment(session_factory, "charge-1")).status == "refunded"
    assert refund_notices(chat) == [
        bot_payments.REFUND_NOTICE.format(stars=80)
        + bot_payments.REFUND_COINS.format(coins="150 coins")
    ]
    # /coins names the entry.
    assert "-150  Refunded purchase" in await chat.send("/coins")


async def test_an_admin_refund_is_refused_once_coins_were_spent(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    await buy_pack(chat, "charge-1")
    await spend(session_factory, 11)
    assert await admin_refund(session_factory, "charge-1") == 1
    assert capsys.readouterr().err.strip() == (
        "Only 149 of the purchase's 150 coins are available, so it can't be refunded."
    )
    assert (await payment(session_factory, "charge-1")).status == "paid"
    assert (await balance(session_factory)).total == 149
    assert await refund_entries(session_factory) == []


async def test_held_coins_are_not_available_to_an_admin_refund(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_pack(chat, "charge-1")
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await coins.hold(session, user.id, 20, "paid_match", "search")
        await session.commit()
    assert await admin_refund(session_factory, "charge-1") == 1
    assert (await payment(session_factory, "charge-1")).status == "paid"


async def test_an_admin_refund_run_again_changes_nothing(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    await buy_pack(chat, "charge-1")
    assert await admin_refund(session_factory, "charge-1") == 0
    assert await admin_refund(session_factory, "charge-1", "again") == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "That purchase is already refunded or being refunded. Nothing changed."
    )
    assert len(await refund_entries(session_factory)) == 1
    assert (await payment(session_factory, "charge-1")).refund_note == "support"
    async with session_factory() as session:
        assert len((await session.execute(select(AdminAuditEntry))).all()) == 1


async def test_a_repeated_confirmation_during_an_admin_refund_gets_the_refund_notice(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    assert await admin_refund(session_factory, "charge-1") == 0
    # Telegram sends the confirmation again: the refund owed is sent at once.
    await chat.paid("charge-1", payload, 80)
    assert (await payment(session_factory, "charge-1")).status == "refunded"
    assert refund_notices(chat) == [
        bot_payments.REFUND_NOTICE.format(stars=80)
        + bot_payments.REFUND_COINS.format(coins="150 coins")
    ]
    assert bot_payments.REFUNDED.format(stars=80) not in notices(chat)


async def test_simultaneous_admin_refunds_take_back_once(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_pack(chat, "charge-1")
    assert sorted(
        await asyncio.gather(*(admin_refund(session_factory, "charge-1") for _ in range(3)))
    ) == [0, 0, 0]
    assert len(await refund_entries(session_factory)) == 1
    assert (await balance(session_factory)).total == 10


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["refund", "999999", "--reason", "x"], "No purchase with that id."),
        (["refund", "1", "--reason", "   "], "Give a reason for the refund."),
    ],
)
async def test_an_admin_refund_needs_a_purchase_and_a_reason(
    chat: Chat,
    session_factory: Sessions,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    error: str,
) -> None:
    await buy_pack(chat, "charge-1")
    assert await cli(session_factory, *args) == 1
    assert capsys.readouterr().err.strip() == error
    assert (await payment(session_factory, "charge-1")).status == "paid"


async def test_the_cli_lists_purchases(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli(session_factory, "purchases", str(USER_ID)) == 0
    assert capsys.readouterr().out.splitlines() == ["No purchases."]
    await buy_pack(chat, "charge-1")
    await buy_vip(chat, "charge-2")
    await spend(session_factory, 3_900)
    await chat.refunded("charge-1", "", 80)
    assert await cli(session_factory, "purchases", str(USER_ID)) == 0
    first, second = (
        await payment(session_factory, "charge-1"),
        await payment(session_factory, "charge-2"),
    )
    assert capsys.readouterr().out.splitlines() == [
        f"#{second.id}\t{second.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC\t780 Stars\t"
        "VIP 180 days + 3750 coins\tpaid",
        f"#{first.id}\t{first.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC\t80 Stars\t150 coins\t"
        "refunded (telegram), 140 coins not recovered",
    ]
    assert await cli(session_factory, "purchases", "424242") == 1


# --- Refunds reported by Telegram ---------------------------------------------------------


async def test_a_telegram_refund_takes_back_what_is_left_and_records_the_shortfall(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    await spend(session_factory, 100)  # 60 left of 160
    await chat.refunded("charge-1", payload, 80)
    found = await payment(session_factory, "charge-1")
    assert (found.status, found.refund_reason, found.refund_shortfall) == (
        "refunded",
        "telegram",
        90,
    )
    assert found.refunded_at is not None
    assert (await balance(session_factory)).total == 0
    assert await refund_entries(session_factory) == [(-60, f"refund:{found.id}")]
    assert refund_notices(chat) == [
        bot_payments.REFUND_NOTICE.format(stars=80)
        + bot_payments.REFUND_COINS.format(coins="60 coins")
    ]
    # Telegram returned the Stars itself; nothing is sent back again.
    assert chat.session.refunds() == []
    assert await run_worker(chat, session_factory) == 0


async def test_a_telegram_refund_leaves_held_coins_alone(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await coins.hold(session, user.id, 20, "paid_match", "search")
        await session.commit()
    await chat.refunded("charge-1", payload, 80)
    assert await balance(session_factory) == coins.Balance(total=20, held=20)
    assert (await payment(session_factory, "charge-1")).refund_shortfall == 10


async def test_a_telegram_refund_with_nothing_left_records_all_as_shortfall(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    await spend(session_factory, 160)
    await chat.refunded("charge-1", payload, 80)
    assert (await payment(session_factory, "charge-1")).refund_shortfall == 150
    assert await refund_entries(session_factory) == []
    assert refund_notices(chat) == [bot_payments.REFUND_NOTICE.format(stars=80)]


async def test_a_repeated_telegram_refund_is_applied_once(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    await chat.refunded("charge-1", payload, 80)
    sent = len(chat.session.requests)
    await chat.refunded("charge-1", payload, 80)
    # The second report is answered with nothing at all, not even an error.
    assert len(chat.session.requests) == sent
    assert len(await refund_entries(session_factory)) == 1
    assert (await balance(session_factory)).total == 10
    assert len(refund_notices(chat)) == 1


async def test_a_refund_naming_another_users_charge_changes_nothing(
    chat: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    other = Chat(bot_settings, redis, session_factory, user_id=7777)
    await complete_onboarding(other)
    await other.refunded("charge-1", payload, 80)
    assert (await payment(session_factory, "charge-1")).status == "paid"
    assert (await balance(session_factory)).total == 160


async def test_a_refund_the_report_recorded_while_the_worker_sent_it_is_told_once(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    assert await admin_refund(session_factory, "charge-1") == 0
    async with session_factory() as session:
        [due] = await payments.claim_refunds(session, datetime.now(UTC), 10)
        await session.commit()
    # Telegram's report lands between the worker's call and its record of it.
    await chat.refunded("charge-1", payload, 80)
    async with session_factory() as session:
        assert await bot_payments.refund(chat.bot, session, due)
    assert len(refund_notices(chat)) == 1


async def test_a_telegram_refund_for_an_unknown_charge_changes_nothing(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_pack(chat, "charge-1")
    await chat.refunded("charge-x", "", 80)
    assert (await payment(session_factory, "charge-1")).status == "paid"
    assert (await balance(session_factory)).total == 160
    assert refund_notices(chat) == []


async def test_telegram_reporting_an_admin_refund_first_tells_the_user_once(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    assert await admin_refund(session_factory, "charge-1") == 0
    # Telegram's report arrives before the worker records its own call.
    await chat.refunded("charge-1", payload, 80)
    found = await payment(session_factory, "charge-1")
    assert (found.status, found.refund_reason) == ("refunded", "admin")
    assert await run_worker(chat, session_factory) == 0
    assert chat.session.refunds() == []
    assert len(refund_notices(chat)) == 1
    assert len(await refund_entries(session_factory)) == 1


async def test_telegram_reporting_an_admin_refund_after_the_worker_changes_nothing(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await buy_pack(chat, "charge-1")
    assert await admin_refund(session_factory, "charge-1") == 0
    assert await run_worker(chat, session_factory) == 1
    await chat.refunded("charge-1", payload, 80)
    assert len(refund_notices(chat)) == 1
    assert len(await refund_entries(session_factory)) == 1


async def test_telegram_reporting_a_refund_owed_for_a_deleted_account(
    chat: Chat, session_factory: Sessions
) -> None:
    await chat.send("/coins")
    await chat.press(PACK_1)
    payload = chat.session.invoices()[-1].payload
    await chat.checkout(payload, 80)
    async with session_factory() as session:
        await users.delete_account(session, (await account(session_factory)).id, datetime.now(UTC))
        await session.commit()
    chat.session.refund_error = "Bad Request: temporarily unavailable"
    await chat.paid("charge-1", payload, 80)
    assert (await payment(session_factory, "charge-1")).status == "refund_due"
    await chat.refunded("charge-1", payload, 80)
    assert (await payment(session_factory, "charge-1")).status == "refunded"
    assert notices(chat).count(bot_payments.REFUNDED.format(stars=80)) == 1
    assert await refund_entries(session_factory) == []


@pytest.mark.parametrize("status", ["active", "banned"])
async def test_a_refund_is_handled_for_any_account_and_is_not_activity(
    chat: Chat, session_factory: Sessions, status: str
) -> None:
    payload = await buy_pack(chat, "charge-1")
    long_ago = datetime.now(UTC) - timedelta(days=3)
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == USER_ID)
            .values(status=status, last_active_at=long_ago)
        )
        await session.commit()
    await chat.refunded("charge-1", payload, 80)
    assert (await payment(session_factory, "charge-1")).status == "refunded"
    assert (await account(session_factory)).last_active_at == long_ago


async def test_a_spend_holding_the_account_runs_before_a_telegram_refund(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_pack(chat, "charge-1")
    async with session_factory() as spending:
        user = await users.get_or_create_user(spending, USER_ID)
        await coins.debit(spending, user.id, 100, "activity_alert", "spent")

        async def report() -> None:
            async with session_factory() as session:
                await payments.record_refund(session, USER_ID, "charge-1", datetime.now(UTC))
                await session.commit()

        refund = asyncio.create_task(report())
        await wait_until_blocked(session_factory)
        await spending.commit()
        await refund
    # The refund saw the spend: it took the 60 left and recorded the rest.
    assert (await balance(session_factory)).total == 0
    assert (await payment(session_factory, "charge-1")).refund_shortfall == 90


async def test_a_spend_waiting_on_a_telegram_refund_finds_the_coins_gone(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_pack(chat, "charge-1")
    async with session_factory() as refunding:
        await payments.record_refund(refunding, USER_ID, "charge-1", datetime.now(UTC))

        async def try_spend() -> None:
            with pytest.raises(coins.InsufficientCoinsError):
                await spend(session_factory, 100)

        spender = asyncio.create_task(try_spend())
        await wait_until_blocked(session_factory)
        await refunding.commit()
        await spender
    assert (await balance(session_factory)).total == 10
    assert (await payment(session_factory, "charge-1")).refund_shortfall is None


# --- VIP passes ---------------------------------------------------------------------------


async def test_refunding_the_only_vip_pass_ends_vip(chat: Chat, session_factory: Sessions) -> None:
    payload = await buy_vip(chat, "charge-1")
    assert payments.is_vip(await account(session_factory), datetime.now(UTC))
    await chat.refunded("charge-1", payload, 780)
    assert not payments.is_vip(await account(session_factory), datetime.now(UTC))
    assert (await balance(session_factory)).total == 10
    assert refund_notices(chat) == [
        bot_payments.REFUND_NOTICE.format(stars=780)
        + bot_payments.REFUND_COINS.format(coins="3750 coins")
        + bot_payments.REFUND_VIP_ENDED
    ]


async def test_refunding_one_of_two_stacked_passes_keeps_the_other(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_vip(chat, "charge-1")
    await buy_vip(chat, "charge-2", EXTEND_VIP)
    before = datetime.now(UTC)
    end = (await account(session_factory)).vip_until
    assert end is not None
    # Refunding the first pass takes its days, not the second's.
    assert await admin_refund(session_factory, "charge-1") == 0
    until = (await account(session_factory)).vip_until
    assert until is not None
    assert end - PASS <= until <= end - PASS + SLACK
    assert before + PASS - SLACK <= until
    assert await run_worker(chat, session_factory) == 1
    assert refund_notices(chat)[-1].endswith(
        bot_payments.REFUND_VIP_LEFT.format(until=vip_date(until))
    )
    # Then the second: VIP ends.
    assert await admin_refund(session_factory, "charge-2") == 0
    assert not payments.is_vip(await account(session_factory), datetime.now(UTC))
    assert (await balance(session_factory)).total == 10


async def test_refunding_the_later_of_two_passes_keeps_the_first(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_vip(chat, "charge-1")
    first_end = (await payment(session_factory, "charge-1")).vip_until
    await buy_vip(chat, "charge-2", EXTEND_VIP)
    assert await admin_refund(session_factory, "charge-2") == 0
    assert (await account(session_factory)).vip_until == first_end


async def test_passes_from_before_an_account_deletion_dont_come_back(
    chat: Chat, session_factory: Sessions
) -> None:
    await buy_vip(chat, "charge-1")
    async with session_factory() as session:
        await users.delete_account(session, (await account(session_factory)).id, datetime.now(UTC))
        await session.commit()
    await complete_onboarding(chat)
    payload = await buy_vip(chat, "charge-2")
    await chat.refunded("charge-2", payload, 780)
    # The first pass ended with the deletion, so nothing is left.
    assert (await account(session_factory)).vip_until is None


def test_vip_end_adds_each_pass_from_the_later_of_its_purchase_and_the_end_so_far() -> None:
    day = datetime(2026, 11, 1, tzinfo=UTC)
    assert payments.vip_end([]) is None
    assert payments.vip_end([(day, 10)]) == day + timedelta(days=10)
    # Stacked: the second starts where the first ends.
    assert payments.vip_end([(day, 10), (day + timedelta(days=2), 5)]) == day + timedelta(days=15)
    # After a gap: the second starts when it was bought.
    assert payments.vip_end([(day, 10), (day + timedelta(days=20), 5)]) == day + timedelta(days=25)


@pytest.mark.parametrize(
    "values",
    [
        # An admin refund with a blank note.
        {"status": "refund_due", "refund_reason": "admin", "refund_note": "  "},
        # A shortfall on anything but a refund Telegram reported.
        {
            "status": "refund_due",
            "refund_reason": "admin",
            "refund_note": "x",
            "refund_shortfall": 5,
        },
    ],
)
async def test_the_database_refuses_inconsistent_refunds(
    chat: Chat, session_factory: Sessions, values: dict[str, object]
) -> None:
    await buy_pack(chat, "charge-1")
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(StarsPayment)
                .where(StarsPayment.telegram_charge_id == "charge-1")
                .values(refund_retry_at=datetime.now(UTC), **values)
            )
