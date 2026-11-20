"""Recording a payment Telegram has that was never recorded (D-068)."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import StarTransaction, TransactionPartnerUser, User
from redis.asyncio import Redis
from sqlalchemy import select
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
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.telegram import RecordingSession, make_bot

Sessions = async_sessionmaker[AsyncSession]
PACK_1 = bot_payments.pack_label(payments.Pack(1, 150, 80))
BUY_VIP = bot_vip.BUY_BUTTON.format(stars=780)
PASS = timedelta(days=180)
SLACK = timedelta(seconds=30)
AT = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
async def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)  # with the 10-coin welcome bonus
    return chat


async def unrecorded(chat: Chat, button: str, command: str, stars: int) -> str:
    """Telegram took the Stars, but the confirmation never reached us; returns the
    invoice's payload."""
    await chat.send(command)
    await chat.press(button)
    payload = chat.session.invoices()[-1].payload
    await chat.checkout(payload, stars)
    return payload


def _user(user_id: int = USER_ID) -> User:
    return User(id=user_id, is_bot=False, first_name="Buyer")


def paid(charge_id: str, payload: str | None, stars: int = 80) -> StarTransaction:
    return StarTransaction(
        id=charge_id,
        amount=stars,
        date=AT,
        source=TransactionPartnerUser(
            transaction_type="invoice_payment", user=_user(), invoice_payload=payload
        ),
    )


def refunded(charge_id: str, stars: int = 80) -> StarTransaction:
    return StarTransaction(
        id=charge_id,
        amount=stars,
        date=AT,
        receiver=TransactionPartnerUser(transaction_type="invoice_payment", user=_user()),
    )


async def record(
    sessions: Sessions, charge_id: str, transactions: list[StarTransaction]
) -> tuple[int, RecordingSession]:
    bot, telegram = make_bot()
    telegram.star_transactions = transactions
    return await admin_cli.run(["record-payment", charge_id], sessions, bot), telegram


def texts(telegram: RecordingSession) -> list[str]:
    return [m.text for m in telegram.requests if isinstance(m, SendMessage)]


async def balance(sessions: Sessions) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, USER_ID)
        return (await coins.balance(session, user.id)).total


async def written(sessions: Sessions) -> tuple[list[tuple[str, str]], list[str], int]:
    """Payments (charge, status), audit actions, and ledger entries."""
    async with sessions() as session:
        stored = await session.execute(
            select(StarsPayment.telegram_charge_id, StarsPayment.status).order_by(StarsPayment.id)
        )
        audit = await session.scalars(select(AdminAuditEntry.action).order_by(AdminAuditEntry.id))
        entries = await session.scalars(select(CoinTransaction.id))
        return [(c, s) for c, s in stored], list(audit), len(entries.all())


async def test_a_missed_coin_pack_is_granted_once_with_its_receipt(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = await unrecorded(chat, PACK_1, "/coins", 80)
    code, telegram = await record(session_factory, "charge-1", [paid("charge-1", payload)])
    assert code == 0
    async with session_factory() as session:
        payment_id = await session.scalar(select(StarsPayment.id))
    assert capsys.readouterr().out.splitlines() == [
        f"Recorded as purchase #{payment_id}: 150 coins granted."
    ]
    assert await balance(session_factory) == 160
    stored, audit, _ = await written(session_factory)
    assert (stored, audit) == ([("charge-1", "paid")], ["payment_recorded"])
    assert texts(telegram) == [bot_payments.CREDITED.format(coins="150 coins")]
    # Running it again changes nothing.
    capsys.readouterr()
    code, telegram = await record(session_factory, "charge-1", [paid("charge-1", payload)])
    assert code == 1
    assert capsys.readouterr().err.strip() == "That payment is already recorded. Nothing changed."
    assert await balance(session_factory) == 160
    assert (await written(session_factory))[1] == ["payment_recorded"]
    assert texts(telegram) == []


async def test_a_missed_vip_pass_is_granted(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = await unrecorded(chat, BUY_VIP, "/vip", 780)
    before = datetime.now(UTC)
    code, telegram = await record(session_factory, "charge-1", [paid("charge-1", payload, 780)])
    assert code == 0
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
    assert user.vip_until is not None
    assert before + PASS <= user.vip_until <= before + PASS + SLACK
    assert await balance(session_factory) == 10 + 3_750
    added = bot_payments.VIP_COINS_ADDED.format(coins="3750 coins")
    assert texts(telegram) == [
        bot_payments.VIP_GRANTED.format(until=vip_date(user.vip_until), coins=added)
    ]
    assert "VIP until" in capsys.readouterr().out


NOT_LISTED = "Telegram doesn't list that charge as a user's payment."
REFUSALS = {
    "not listed": NOT_LISTED,
    "only a refund listed": NOT_LISTED,
    "refunded at Telegram": (
        "Telegram already refunded that payment, so there is nothing to record."
    ),
    "unmatched invoice": (
        "That payment matches none of our invoices, so it can't be recorded; handle it manually."
    ),
}


def listed(case: str, payload: str) -> list[StarTransaction]:
    return {
        "not listed": [],
        "only a refund listed": [refunded("charge-1")],
        "refunded at Telegram": [paid("charge-1", payload), refunded("charge-1")],
        "unmatched invoice": [paid("charge-1", "inv:999999")],
    }[case]


@pytest.mark.parametrize("case", list(REFUSALS))
async def test_refused_cases_write_nothing(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    payload = await unrecorded(chat, PACK_1, "/coins", 80)
    before = await written(session_factory)
    capsys.readouterr()
    code, telegram = await record(session_factory, "charge-1", listed(case, payload))
    assert code == 1
    assert capsys.readouterr().err.strip() == REFUSALS[case]
    assert await written(session_factory) == before
    assert texts(telegram) == []


async def test_a_payment_for_a_deleted_account_is_refunded(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = await unrecorded(chat, PACK_1, "/coins", 80)
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await users.delete_account(session, user.id, datetime.now(UTC))
        await session.commit()
    code, telegram = await record(session_factory, "charge-1", [paid("charge-1", payload)])
    assert code == 0
    assert "the account was deleted" in capsys.readouterr().out
    [refund] = telegram.refunds()
    assert (refund.user_id, refund.telegram_payment_charge_id) == (USER_ID, "charge-1")
    stored, audit, _ = await written(session_factory)
    assert (stored, audit) == ([("charge-1", "refunded")], ["payment_recorded"])
    assert texts(telegram) == [bot_payments.REFUNDED.format(stars=80)]


async def test_a_receipt_that_cant_be_sent_leaves_the_payment_recorded(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = await unrecorded(chat, PACK_1, "/coins", 80)
    bot, telegram = make_bot()
    telegram.star_transactions = [paid("charge-1", payload)]
    telegram.send_error = TelegramForbiddenError(
        method=SendMessage(chat_id=USER_ID, text="x"), message="Forbidden: bot was blocked"
    )
    assert await admin_cli.run(["record-payment", "charge-1"], session_factory, bot) == 0
    assert "Couldn't tell the user" in capsys.readouterr().err
    assert await balance(session_factory) == 160


async def test_record_payment_needs_a_bot(session_factory: Sessions) -> None:
    with pytest.raises(ValueError, match="record-payment needs a bot"):
        await admin_cli.run(["record-payment", "charge-1"], session_factory)
