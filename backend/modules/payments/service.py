"""Coin packs and the VIP pass bought with Telegram Stars (D-061, D-062).

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids are internal `users.id` values unless named `telegram_user_id`.

The flow, following Telegram's rules for digital goods:
1. `create_invoice` stores what is offered; the invoice sent to Telegram carries only
   its reference (`payload`).
2. Telegram asks before taking the Stars; `checkout_refusal` says whether to proceed.
3. Telegram confirms the payment; `fulfil` records it once per Telegram charge and
   credits the coins, and for a VIP pass extends VIP, in the same transaction. Only
   this confirmation grants anything.
4. A payment that can't be fulfilled is refunded: the bot tries at once, and the
   worker retries any refund still owed (`claim_refunds`, `mark_refunded`).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.admin.settings import SettingKey
from backend.modules.coins import service as coins
from backend.modules.payments.models import StarsInvoice, StarsPayment
from backend.modules.users import service as users
from backend.modules.users.models import User

logger = logging.getLogger(__name__)

CURRENCY = "XTR"  # Telegram Stars
# How long an invoice can be paid at the price and coins it showed.
INVOICE_VALID = timedelta(hours=1)
# How long to wait before trying a refund still owed again; the first wait also leaves
# the bot's own immediate attempt alone.
REFUND_RETRY = timedelta(minutes=5)
PAYLOAD_PREFIX = "inv:"
MAX_ID = 2**63 - 1  # ids are PostgreSQL bigints
PACK_KEYS = (
    (SettingKey.PACK_1_COINS, SettingKey.PACK_1_STARS),
    (SettingKey.PACK_2_COINS, SettingKey.PACK_2_STARS),
    (SettingKey.PACK_3_COINS, SettingKey.PACK_3_STARS),
    (SettingKey.PACK_4_COINS, SettingKey.PACK_4_STARS),
)


@dataclass(frozen=True)
class Pack:
    number: int
    coins: int
    stars: int


def packs(values: dict[SettingKey, int]) -> list[Pack]:
    """The packs on offer, from the admin settings; a pack with 0 coins is hidden."""
    return [
        Pack(number, values[coins_key], values[stars_key])
        for number, (coins_key, stars_key) in enumerate(PACK_KEYS, start=1)
        if values[coins_key] > 0
    ]


def pack(values: dict[SettingKey, int], number: int) -> Pack | None:
    return next((p for p in packs(values) if p.number == number), None)


@dataclass(frozen=True)
class VipPass:
    stars: int
    days: int
    coins: int


def vip_pass(values: dict[SettingKey, int]) -> VipPass:
    """The VIP pass on offer, from the admin settings (D-062)."""
    return VipPass(
        values[SettingKey.VIP_STARS], values[SettingKey.VIP_DAYS], values[SettingKey.VIP_COINS]
    )


def is_vip(user: User, now: datetime) -> bool:
    """VIP while the stored end date is in the future, so expiry needs no job."""
    return user.vip_until is not None and user.vip_until > now


def can_buy_vip(user: User, now: datetime, days: int) -> bool:
    """A pass can be bought while at most one pass's length of VIP is left, so VIP never
    runs more than two passes ahead."""
    return user.vip_until is None or user.vip_until - now <= timedelta(days=days)


async def create_invoice(session: AsyncSession, user_id: int, offer: Pack | VipPass) -> int:
    """Store an invoice for `offer`; returns its id."""
    values = (
        {"product": "vip", "days": offer.days}
        if isinstance(offer, VipPass)
        else {"product": "coins"}
    )
    invoice_id = await session.scalar(
        insert(StarsInvoice)
        .values(user_id=user_id, stars=offer.stars, coins=offer.coins, **values)
        .returning(StarsInvoice.id)
    )
    return int(invoice_id)  # type: ignore[arg-type]


def payload(invoice_id: int) -> str:
    return f"{PAYLOAD_PREFIX}{invoice_id}"


def invoice_id_from(payload_text: str) -> int | None:
    if not payload_text.startswith(PAYLOAD_PREFIX):
        return None
    number = payload_text.removeprefix(PAYLOAD_PREFIX)
    if not number.isdecimal() or len(number) > len(str(MAX_ID)):
        return None
    value = int(number)
    return value if value <= MAX_ID else None


class Refusal(StrEnum):
    UNKNOWN = "unknown"  # not an invoice of ours for this user, or from before a deletion
    EXPIRED = "expired"
    MISMATCH = "mismatch"  # a different currency or amount from the stored invoice
    UNAVAILABLE = "unavailable"  # the account can't buy right now
    TERMS = "terms"  # the current terms aren't accepted
    VIP_LIMIT = "vip_limit"  # more than a pass's length of VIP is left


async def _invoice_for(
    session: AsyncSession, telegram_user_id: int, payload_text: str
) -> tuple[StarsInvoice, User] | None:
    invoice_id = invoice_id_from(payload_text)
    if invoice_id is None:
        return None
    row = (
        await session.execute(
            select(StarsInvoice, User)
            .join(User, User.id == StarsInvoice.user_id)
            .where(StarsInvoice.id == invoice_id, User.telegram_user_id == telegram_user_id)
        )
    ).first()
    return None if row is None else (row[0], row[1])


async def checkout_refusal(
    session: AsyncSession,
    telegram_user_id: int,
    payload_text: str,
    currency: str,
    stars: int,
    now: datetime,
    terms_version: str,
) -> Refusal | None:
    """Why Telegram should not take this payment, or None to let it proceed."""
    found = await _invoice_for(session, telegram_user_id, payload_text)
    if found is None:
        return Refusal.UNKNOWN
    invoice, user = found
    if user.deleted_at is not None and invoice.created_at <= user.deleted_at:
        return Refusal.UNKNOWN
    if currency != CURRENCY or stars != invoice.stars:
        return Refusal.MISMATCH
    if now - invoice.created_at > INVOICE_VALID:
        return Refusal.EXPIRED
    if user.status != "active":
        return Refusal.UNAVAILABLE
    if not await users.has_accepted_terms(session, user.id, terms_version):
        return Refusal.TERMS
    if invoice.days is not None and not can_buy_vip(user, now, invoice.days):
        return Refusal.VIP_LIMIT
    return None


class Outcome(StrEnum):
    CREDITED = "credited"
    REFUND_DUE = "refund_due"  # the account was deleted; the Stars must go back
    ALREADY = "already"  # this charge was handled before


@dataclass(frozen=True)
class Fulfilled:
    outcome: Outcome
    payment_id: int
    coins: int
    # The payment's status now: for ALREADY, "refund_due" means a refund is still owed.
    status: str
    product: str = "coins"
    # For a granted VIP pass: the VIP end date it produced.
    vip_until: datetime | None = None


class UnknownPaymentError(Exception):
    """A confirmed payment that doesn't match an invoice of ours for its payer."""


async def fulfil(
    session: AsyncSession,
    telegram_user_id: int,
    charge_id: str,
    payload_text: str,
    currency: str,
    stars: int,
    now: datetime,
) -> Fulfilled:
    """Record a confirmed payment and grant what it bought, once per Telegram charge:
    the coins, and for a VIP pass its days, added to the current VIP end date if VIP
    is still running (D-062).

    The account row is locked first, as account deletion and every ledger write do, so
    a deletion either finishes before (and the payment is refunded) or waits. A
    restricted account is still granted the purchase: the Stars were taken, and the
    coins stay with the account like any other balance. The VIP stacking limit is
    checked at checkout only, so every pass paid meanwhile is granted, even if
    that puts VIP more than two passes ahead.

    Raises UnknownPaymentError if the payment can't be matched to its invoice, which
    checkout prevents; the caller must report it, since the Stars were taken.
    """
    found = await _invoice_for(session, telegram_user_id, payload_text)
    if found is None or currency != CURRENCY or stars != found[0].stars:
        raise UnknownPaymentError(charge_id)
    invoice = found[0]
    user = (
        await session.execute(
            select(User)
            .where(User.id == invoice.user_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    deleted = user.status == "deleted" or (
        user.deleted_at is not None and invoice.created_at <= user.deleted_at
    )
    vip_until: datetime | None = None
    if invoice.days is not None and not deleted:
        start = user.vip_until if user.vip_until is not None and user.vip_until > now else now
        vip_until = start + timedelta(days=invoice.days)
    payment_id = await session.scalar(
        insert(StarsPayment)
        .values(
            telegram_charge_id=charge_id,
            user_id=user.id,
            invoice_id=invoice.id,
            stars=stars,
            status="refund_due" if deleted else "paid",
            refund_reason="account_deleted" if deleted else None,
            refund_retry_at=now + REFUND_RETRY if deleted else None,
            vip_until=vip_until,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["telegram_charge_id"])
        .returning(StarsPayment.id)
    )
    if payment_id is None:
        existing = (
            await session.execute(
                select(StarsPayment.id, StarsPayment.status, StarsPayment.vip_until).where(
                    StarsPayment.telegram_charge_id == charge_id
                )
            )
        ).one()
        return Fulfilled(
            Outcome.ALREADY,
            int(existing.id),
            invoice.coins,
            existing.status,
            invoice.product,
            existing.vip_until,
        )
    if deleted:
        return Fulfilled(
            Outcome.REFUND_DUE, int(payment_id), invoice.coins, "refund_due", invoice.product
        )
    key = f"payment:{payment_id}"
    if invoice.product == "vip":
        if invoice.coins:
            await coins.credit(session, user.id, invoice.coins, "vip_coins", key)
        await session.execute(update(User).where(User.id == user.id).values(vip_until=vip_until))
    else:
        await coins.credit(session, user.id, invoice.coins, "coin_purchase", key)
    return Fulfilled(
        Outcome.CREDITED, int(payment_id), invoice.coins, "paid", invoice.product, vip_until
    )


async def mark_refunded(session: AsyncSession, payment_id: int, now: datetime) -> bool:
    """Record that a refund owed was made; returns whether this call recorded it."""
    done = await session.scalar(
        update(StarsPayment)
        .where(StarsPayment.id == payment_id, StarsPayment.status == "refund_due")
        .values(status="refunded", refunded_at=now, refund_retry_at=None)
        .returning(StarsPayment.id)
    )
    return done is not None


@dataclass(frozen=True)
class RefundDue:
    payment_id: int
    telegram_user_id: int
    charge_id: str
    stars: int


async def claim_refunds(session: AsyncSession, now: datetime, limit: int) -> list[RefundDue]:
    """Claim up to `limit` refunds owed whose retry time has come, moving each one's next
    retry `REFUND_RETRY` ahead. Commit before calling Telegram, so a concurrent worker
    skips them and none is tried twice at once."""
    due = (
        select(StarsPayment.id)
        .where(StarsPayment.status == "refund_due", StarsPayment.refund_retry_at <= now)
        .order_by(StarsPayment.refund_retry_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = (
        await session.execute(
            update(StarsPayment)
            .where(StarsPayment.id.in_(due.scalar_subquery()), User.id == StarsPayment.user_id)
            .values(refund_retry_at=now + REFUND_RETRY)
            .returning(
                StarsPayment.id,
                User.telegram_user_id,
                StarsPayment.telegram_charge_id,
                StarsPayment.stars,
            )
        )
    ).all()
    return [
        RefundDue(int(row.id), row.telegram_user_id, row.telegram_charge_id, row.stars)
        for row in sorted(claimed, key=lambda row: row.id)
    ]
