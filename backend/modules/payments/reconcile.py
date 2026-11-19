"""Comparing stored Stars payments with Telegram's list of the bot's Stars transactions
(D-066). Read-only: it reports differences and never changes payments or balances.

Telegram lists a user's payment under its charge id, and a refund of it under the same
id. Only those are compared; anything else Telegram lists (withdrawals, for example) is
not a purchase. It is read-only; see D-066 for which differences admin commands can fix.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.payments.models import StarsPayment
from backend.modules.users.models import User


@dataclass(frozen=True)
class TelegramTransaction:
    """A user's Stars payment or its refund, as Telegram lists it."""

    charge_id: str
    telegram_user_id: int
    stars: int
    refund: bool  # False for the payment, True for its refund
    at: datetime


class Difference(StrEnum):
    NOT_RECORDED = "paid at Telegram, not recorded here"
    NOT_AT_TELEGRAM = "recorded here, not at Telegram"
    REFUNDED_AT_TELEGRAM = "refunded at Telegram, still paid here"
    NOT_REFUNDED_AT_TELEGRAM = "refunded here, no refund at Telegram"
    REFUND_OWED_DONE = "refund owed here, already refunded at Telegram"
    MISMATCH = "different at Telegram"


@dataclass(frozen=True)
class Found:
    difference: Difference
    charge_id: str
    telegram_user_id: int
    detail: str = ""


@dataclass(frozen=True)
class Report:
    differences: list[Found]
    payments: int  # user payments Telegram listed
    refunds_owed: int  # refunds owed here and not yet made at Telegram: the worker's job


@dataclass(frozen=True)
class _Stored:
    telegram_user_id: int
    stars: int
    status: str


async def _stored(session: AsyncSession) -> dict[str, _Stored]:
    rows = await session.execute(
        select(
            StarsPayment.telegram_charge_id,
            User.telegram_user_id,
            StarsPayment.stars,
            StarsPayment.status,
        ).join(User, User.id == StarsPayment.user_id)
    )
    return {
        charge: _Stored(int(telegram_user_id), int(stars), status)
        for charge, telegram_user_id, stars, status in rows
    }


def _compare(
    charge: str, paid: TelegramTransaction, refund: TelegramTransaction | None, ours: _Stored
) -> Found | None:
    refunded = refund is not None
    # A refund goes back in full to the payer, so it must match the payment too. Both are
    # shown when both differ.
    differing = [
        (listed, what)
        for listed, what in ((paid, "Telegram"), (refund, "Telegram's refund"))
        if listed is not None
        and (ours.telegram_user_id, ours.stars) != (listed.telegram_user_id, listed.stars)
    ]
    if differing:
        shown = [f"{what}: user {t.telegram_user_id}, {t.stars} Stars" for t, what in differing]
        return Found(
            Difference.MISMATCH,
            charge,
            differing[0][0].telegram_user_id,
            "; ".join([*shown, f"here: user {ours.telegram_user_id}, {ours.stars} Stars"]),
        )
    if ours.status == "paid" and refunded:
        return Found(Difference.REFUNDED_AT_TELEGRAM, charge, paid.telegram_user_id)
    if ours.status == "refunded" and not refunded:
        return Found(Difference.NOT_REFUNDED_AT_TELEGRAM, charge, paid.telegram_user_id)
    if ours.status == "refund_due" and refunded:
        return Found(Difference.REFUND_OWED_DONE, charge, paid.telegram_user_id)
    return None


async def reconcile(session: AsyncSession, transactions: list[TelegramTransaction]) -> Report:
    """Compare Telegram's transactions with every stored payment."""
    paid = {t.charge_id: t for t in transactions if not t.refund}
    refunds = {t.charge_id: t for t in transactions if t.refund}
    stored = await _stored(session)
    differences: list[Found] = []
    owed = 0
    for charge, payment in sorted(paid.items(), key=lambda item: item[1].at):
        ours = stored.get(charge)
        if ours is None:
            differences.append(
                Found(
                    Difference.NOT_RECORDED,
                    charge,
                    payment.telegram_user_id,
                    f"{payment.stars} Stars at {payment.at:%Y-%m-%d %H:%M} UTC",
                )
            )
            continue
        found = _compare(charge, payment, refunds.get(charge), ours)
        if found is not None:
            differences.append(found)
        elif ours.status == "refund_due":
            owed += 1
    for charge, ours in sorted(stored.items()):
        if charge not in paid:
            differences.append(
                Found(Difference.NOT_AT_TELEGRAM, charge, ours.telegram_user_id, ours.status)
            )
    return Report(differences, len(paid), owed)
