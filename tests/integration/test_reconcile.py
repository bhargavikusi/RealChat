"""Reconciling stored payments with Telegram's Stars transactions (D-066)."""

from datetime import UTC, datetime

import pytest
from aiogram.methods import GetStarTransactions
from aiogram.types import (
    StarTransaction,
    TransactionPartnerFragment,
    TransactionPartnerUser,
    User,
)
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps import admin_cli
from apps.bot import payments as bot_payments
from backend.config.settings import BotSettings
from backend.modules.coins.models import CoinTransaction
from backend.modules.payments import service as payments
from backend.modules.payments.models import StarsPayment
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.telegram import make_bot

Sessions = async_sessionmaker[AsyncSession]
PACK_1 = bot_payments.pack_label(payments.Pack(1, 150, 80))
AT = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
OTHER_ID = 8888


@pytest.fixture
async def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    return chat


async def buy_pack(chat: Chat, charge_id: str) -> str:
    await chat.send("/coins")
    await chat.press(PACK_1)
    payload = chat.session.invoices()[-1].payload
    await chat.checkout(payload, 80)
    await chat.paid(charge_id, payload, 80)
    return payload


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Buyer")


def paid(charge_id: str, stars: int = 80, user_id: int = USER_ID) -> StarTransaction:
    return StarTransaction(
        id=charge_id,
        amount=stars,
        date=AT,
        source=TransactionPartnerUser(transaction_type="invoice_payment", user=_user(user_id)),
    )


def refunded(charge_id: str, stars: int = 80, user_id: int = USER_ID) -> StarTransaction:
    return StarTransaction(
        id=charge_id,
        amount=stars,
        date=AT,
        receiver=TransactionPartnerUser(transaction_type="invoice_payment", user=_user(user_id)),
    )


def withdrawal(number: int) -> StarTransaction:
    return StarTransaction(
        id=f"withdrawal-{number}", amount=1, date=AT, receiver=TransactionPartnerFragment()
    )


async def run(sessions: Sessions, transactions: list[StarTransaction]) -> tuple[int, int]:
    """Run `reconcile` against these transactions; returns (exit code, pages read)."""
    bot, telegram = make_bot()
    telegram.star_transactions = transactions
    code = await admin_cli.run(["reconcile"], sessions, bot)
    pages = sum(1 for m in telegram.requests if isinstance(m, GetStarTransactions))
    return code, pages


async def stored_rows(sessions: Sessions) -> tuple[list[tuple[str, str]], int]:
    async with sessions() as session:
        rows = await session.execute(
            select(StarsPayment.telegram_charge_id, StarsPayment.status).order_by(StarsPayment.id)
        )
        entries = await session.scalar(select(func.count()).select_from(CoinTransaction))
        return [(charge, status) for charge, status in rows], int(entries or 0)


async def test_matching_records_report_nothing(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    await buy_pack(chat, "charge-1")
    payload = await buy_pack(chat, "charge-2")
    await chat.refunded("charge-2", payload, 80)
    capsys.readouterr()
    code, _ = await run(session_factory, [paid("charge-1"), paid("charge-2"), refunded("charge-2")])
    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "Checked 2 payments at Telegram: no differences, no refunds still owed for the "
        "worker to send."
    ]


async def test_each_difference_is_reported_and_nothing_is_written(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    for charge in ("same", "refunded-there", "owed-done", "owed", "missing-there", "amount"):
        await buy_pack(chat, charge)
    payload = await buy_pack(chat, "refunded-here")
    await chat.refunded("refunded-here", payload, 80)
    for charge in ("owed-done", "owed"):
        async with session_factory() as session:
            found = await session.scalar(
                select(StarsPayment.id).where(StarsPayment.telegram_charge_id == charge)
            )
        assert await admin_cli.run(["refund", str(found), "--reason", "test"], session_factory) == 0
    before = await stored_rows(session_factory)
    capsys.readouterr()
    code, _ = await run(
        session_factory,
        [
            paid("same"),
            paid("refunded-there"),
            refunded("refunded-there"),
            paid("owed-done"),
            refunded("owed-done"),
            paid("owed"),
            paid("refunded-here"),
            paid("amount", stars=81),
            paid("unknown", user_id=OTHER_ID),
            # Not purchases: a gift sent to a user, a paid media payment, and a withdrawal.
            refunded("gift-1", user_id=OTHER_ID),
            StarTransaction(
                id="media-1",
                amount=5,
                date=AT,
                source=TransactionPartnerUser(
                    transaction_type="paid_media_payment", user=_user(OTHER_ID)
                ),
            ),
            withdrawal(1),
        ],
    )
    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"refunded-there\tuser {USER_ID}\trefunded at Telegram, still paid here",
        f"owed-done\tuser {USER_ID}\trefund owed here, already refunded at Telegram",
        f"refunded-here\tuser {USER_ID}\trefunded here, no refund at Telegram",
        f"amount\tuser {USER_ID}\tdifferent at Telegram (Telegram: user {USER_ID}, 81 Stars; "
        f"here: user {USER_ID}, 80 Stars)",
        f"unknown\tuser {OTHER_ID}\tpaid at Telegram, not recorded here "
        "(80 Stars at 2026-11-19 12:00 UTC)",
        f"missing-there\tuser {USER_ID}\trecorded here, not at Telegram (paid)",
        "Checked 7 payments at Telegram: 6 differences, 1 refund still owed for the worker "
        "to send.",
        "A payment confirmed in the last few minutes may show as not recorded until the bot "
        "records it.",
    ]
    # Read-only.
    assert await stored_rows(session_factory) == before


async def test_a_payment_listed_for_another_user_is_a_difference(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    await buy_pack(chat, "charge-1")
    capsys.readouterr()
    await run(session_factory, [paid("charge-1", user_id=OTHER_ID)])
    assert capsys.readouterr().out.splitlines()[0] == (
        f"charge-1\tuser {OTHER_ID}\tdifferent at Telegram (Telegram: user {OTHER_ID}, "
        f"80 Stars; here: user {USER_ID}, 80 Stars)"
    )


async def test_every_page_is_read(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    await buy_pack(chat, "charge-1")
    capsys.readouterr()
    # 230 withdrawals first, so the payment is on the third page.
    code, pages = await run(
        session_factory, [*(withdrawal(n) for n in range(230)), paid("charge-1")]
    )
    assert (code, pages) == (0, 3)
    assert capsys.readouterr().out.splitlines() == [
        "Checked 1 payment at Telegram: no differences, no refunds still owed for the "
        "worker to send."
    ]


async def test_a_full_last_page_is_followed_by_an_empty_one(
    chat: Chat, session_factory: Sessions
) -> None:
    code, pages = await run(session_factory, [withdrawal(n) for n in range(200)])
    assert (code, pages) == (0, 3)


async def test_reconcile_needs_a_bot(session_factory: Sessions) -> None:
    with pytest.raises(ValueError, match="reconcile needs a bot"):
        await admin_cli.run(["reconcile"], session_factory)


async def test_a_refund_to_another_user_or_for_other_stars_is_a_difference(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    for charge in ("other-user", "other-stars"):
        payload = await buy_pack(chat, charge)
        await chat.refunded(charge, payload, 80)
    capsys.readouterr()
    await run(
        session_factory,
        [
            paid("other-user"),
            refunded("other-user", user_id=OTHER_ID),
            paid("other-stars"),
            refunded("other-stars", stars=40),
        ],
    )
    assert capsys.readouterr().out.splitlines()[:2] == [
        f"other-user\tuser {OTHER_ID}\tdifferent at Telegram (Telegram's refund: user "
        f"{OTHER_ID}, 80 Stars; here: user {USER_ID}, 80 Stars)",
        f"other-stars\tuser {USER_ID}\tdifferent at Telegram (Telegram's refund: user "
        f"{USER_ID}, 40 Stars; here: user {USER_ID}, 80 Stars)",
    ]


async def test_a_payment_and_refund_that_both_differ_are_both_shown(
    chat: Chat, session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = await buy_pack(chat, "charge-1")
    await chat.refunded("charge-1", payload, 80)
    capsys.readouterr()
    await run(session_factory, [paid("charge-1", stars=81), refunded("charge-1", user_id=OTHER_ID)])
    assert capsys.readouterr().out.splitlines()[0] == (
        f"charge-1\tuser {USER_ID}\tdifferent at Telegram (Telegram: user {USER_ID}, 81 Stars; "
        f"Telegram's refund: user {OTHER_ID}, 80 Stars; here: user {USER_ID}, 80 Stars)"
    )
