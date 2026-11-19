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
5. A fulfilled purchase is refunded by an admin (`refund_by_admin`, sent by the worker as
   in 4), or reported refunded by Telegram (`record_refund`, D-065). Either takes back
   what it gave: its coins, by one ledger entry under `refund_key` when there are any,
   and for a VIP pass its days, by working VIP out again as if it was never bought.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import ColumnElement, select, update
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


async def vip_users(session: AsyncSession, user_ids: Iterable[int], now: datetime) -> set[int]:
    """Which of these users are VIP now, for the badge and matching priority (D-063)."""
    ids = list(user_ids)
    if not ids:
        return set()
    rows = await session.scalars(select(User.id).where(User.id.in_(ids), User.vip_until > now))
    return {int(user_id) for user_id in rows}


async def set_ghost_mode(session: AsyncSession, user_id: int, on: bool, now: datetime) -> bool:
    """Turn Ghost mode on or off (D-064); returns False, changing nothing, unless the
    user is VIP now. One conditional update, so VIP ending meanwhile can't be missed."""
    changed = await session.scalar(
        update(User)
        .where(User.id == user_id, User.vip_until > now)
        .values(ghost_mode=on)
        .returning(User.id)
    )
    return changed is not None


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
    # Why a refund is owed or was made, for any status but "paid".
    refund_reason: str | None = None


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
                select(
                    StarsPayment.id,
                    StarsPayment.status,
                    StarsPayment.vip_until,
                    StarsPayment.refund_reason,
                ).where(StarsPayment.telegram_charge_id == charge_id)
            )
        ).one()
        return Fulfilled(
            Outcome.ALREADY,
            int(existing.id),
            invoice.coins,
            existing.status,
            invoice.product,
            existing.vip_until,
            existing.refund_reason,
        )
    if deleted:
        return Fulfilled(
            Outcome.REFUND_DUE,
            int(payment_id),
            invoice.coins,
            "refund_due",
            invoice.product,
            refund_reason="account_deleted",
        )
    key = f"payment:{payment_id}"
    if invoice.product == "vip":
        if invoice.coins:
            await coins.credit(session, user.id, invoice.coins, "vip_coins", key)
        # A new VIP period starts with Ghost mode on; an extension keeps the choice (D-064).
        new_period = user.vip_until is None or user.vip_until <= now
        values = {"vip_until": vip_until, **({"ghost_mode": True} if new_period else {})}
        await session.execute(update(User).where(User.id == user.id).values(**values))
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
    reason: str  # a `REFUND_REASONS` value


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
                StarsPayment.refund_reason,
            )
        )
    ).all()
    return [
        RefundDue(
            int(row.id), row.telegram_user_id, row.telegram_charge_id, row.stars, row.refund_reason
        )
        for row in sorted(claimed, key=lambda row: row.id)
    ]


# --- Refunding a fulfilled purchase (D-065) ----------------------------------------------


def refund_key(payment_id: int) -> str:
    """The ledger key of the entry that takes a refunded purchase's coins back."""
    return f"refund:{payment_id}"


class RefundRefusal(StrEnum):
    NOT_FOUND = "not_found"
    NOT_PAID = "not_paid"  # already refunded, or a refund is already owed
    COINS_SPENT = "coins_spent"


class RefundRefusedError(Exception):
    def __init__(self, refusal: RefundRefusal, available: int = 0, needed: int = 0) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal
        self.available = available
        self.needed = needed


@dataclass(frozen=True)
class Reversed:
    """What a refund took back."""

    coins: int  # coins removed
    shortfall: int  # coins it couldn't remove, because they were spent
    vip_days_removed: bool  # for a VIP pass: whether VIP was shortened


async def _locked_payment(
    session: AsyncSession, where: ColumnElement[bool]
) -> tuple[StarsPayment, StarsInvoice] | None:
    row = (
        await session.execute(
            select(StarsPayment, StarsInvoice)
            .join(StarsInvoice, StarsInvoice.id == StarsPayment.invoice_id)
            .where(where)
            .with_for_update(of=StarsPayment)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


def vip_end(passes: Iterable[tuple[datetime, int]]) -> datetime | None:
    """The VIP end date that these passes, as (bought at, days) in purchase order, give
    together: each adds its days to the later of when it was bought and the end so far,
    as `fulfil` does. None without any."""
    end: datetime | None = None
    for bought_at, days in passes:
        end = (bought_at if end is None or end < bought_at else end) + timedelta(days=days)
    return end


async def _vip_without(session: AsyncSession, user: User, payment_id: int) -> datetime | None:
    """VIP's end date as if this pass had never been bought (D-065): rebuilt from the
    user's other granted passes that stand, since their account was last deleted. Time
    already used counts against those passes."""
    query = (
        select(StarsPayment.created_at, StarsInvoice.days)
        .join(StarsInvoice, StarsInvoice.id == StarsPayment.invoice_id)
        .where(
            StarsPayment.user_id == user.id,
            StarsPayment.id != payment_id,
            StarsPayment.status == "paid",
            StarsPayment.vip_until.is_not(None),
        )
        .order_by(StarsPayment.created_at, StarsPayment.id)
    )
    if user.deleted_at is not None:
        query = query.where(StarsPayment.created_at > user.deleted_at)
    rows = await session.execute(query)
    return vip_end((bought_at, days) for bought_at, days in rows if days is not None)


async def _reverse(
    session: AsyncSession,
    payment: StarsPayment,
    invoice: StarsInvoice,
    *,
    all_coins: bool,
) -> Reversed:
    """Take back what the purchase gave. With `all_coins`, refuse unless every coin it
    added is still available; otherwise take what is available (held coins stay put)
    and report the rest as a shortfall."""
    user = (
        await session.execute(
            select(User)
            .where(User.id == payment.user_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    available = (await coins.balance(session, user.id)).available
    if all_coins and available < invoice.coins:
        raise RefundRefusedError(RefundRefusal.COINS_SPENT, available, invoice.coins)
    taken = min(available, invoice.coins)
    if taken:
        await coins.debit(session, user.id, taken, "purchase_refund", refund_key(payment.id))
    removed = False
    if invoice.product == "vip" and payment.vip_until is not None and user.vip_until is not None:
        rebuilt = await _vip_without(session, user, payment.id)
        # Never longer than it is now.
        until = None if rebuilt is None else min(rebuilt, user.vip_until)
        if until != user.vip_until:
            await session.execute(update(User).where(User.id == user.id).values(vip_until=until))
            removed = True
    return Reversed(taken, invoice.coins - taken, removed)


async def refund_by_admin(
    session: AsyncSession, payment_id: int, note: str, now: datetime
) -> Reversed:
    """Take back a paid purchase and mark its Stars owed, for the worker to refund.

    Refused unless the purchase is paid and every coin it added is still available, so
    nothing is ever taken back only in part. The payment row is locked first, so a
    second refund or Telegram's own report waits and then finds it no longer paid.
    """
    found = await _locked_payment(session, StarsPayment.id == payment_id)
    if found is None:
        raise RefundRefusedError(RefundRefusal.NOT_FOUND)
    payment, invoice = found
    if payment.status != "paid":
        raise RefundRefusedError(RefundRefusal.NOT_PAID)
    done = await _reverse(session, payment, invoice, all_coins=True)
    payment.status = "refund_due"
    payment.refund_reason = "admin"
    payment.refund_note = note
    payment.refund_retry_at = now  # the worker's next pass sends it
    return done


class RefundOutcome(StrEnum):
    REVERSED = "reversed"  # a paid purchase, taken back now
    RECORDED = "recorded"  # a refund that was owed is now recorded as made
    ALREADY = "already"  # recorded before
    UNKNOWN = "unknown"  # no payment of ours with that charge


@dataclass(frozen=True)
class RefundRecorded:
    outcome: RefundOutcome
    payment_id: int = 0
    reason: str | None = None
    taken: Reversed | None = None  # for REVERSED


async def record_refund(
    session: AsyncSession, telegram_user_id: int, charge_id: str, now: datetime
) -> RefundRecorded:
    """Telegram reports the charge refunded. Applied once per charge, under the payment
    row's lock: a paid purchase is taken back as far as its coins are available, the
    rest recorded as a shortfall; a refund that was owed is recorded as made."""
    found = await _locked_payment(
        session,
        (StarsPayment.telegram_charge_id == charge_id)
        & (StarsPayment.user_id == User.id)
        & (User.telegram_user_id == telegram_user_id),
    )
    if found is None:
        return RefundRecorded(RefundOutcome.UNKNOWN)
    payment, invoice = found
    if payment.status == "refunded":
        return RefundRecorded(RefundOutcome.ALREADY, payment.id, payment.refund_reason)
    if payment.status == "refund_due":
        await mark_refunded(session, payment.id, now)
        return RefundRecorded(RefundOutcome.RECORDED, payment.id, payment.refund_reason)
    taken = await _reverse(session, payment, invoice, all_coins=False)
    payment.status = "refunded"
    payment.refund_reason = "telegram"
    payment.refunded_at = now
    payment.refund_shortfall = taken.shortfall or None
    if taken.shortfall:
        logger.warning("a refunded purchase's coins were partly spent")
    return RefundRecorded(RefundOutcome.REVERSED, payment.id, "telegram", taken)


@dataclass(frozen=True)
class RefundSummary:
    """What a user is told about a refund of a fulfilled purchase."""

    stars: int
    product: str
    coins: int  # coins taken back
    vip_until: datetime | None  # for a VIP pass: the user's VIP end now, if still VIP


async def refund_summary(session: AsyncSession, payment_id: int, now: datetime) -> RefundSummary:
    payment, invoice = (
        await session.execute(
            select(StarsPayment, StarsInvoice)
            .join(StarsInvoice, StarsInvoice.id == StarsPayment.invoice_id)
            .where(StarsPayment.id == payment_id)
        )
    ).one()
    entry = await coins.recorded(session, refund_key(payment_id))
    user = (
        await session.execute(
            select(User).where(User.id == payment.user_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    return RefundSummary(
        payment.stars,
        invoice.product,
        0 if entry is None else -entry.amount,
        user.vip_until if is_vip(user, now) else None,
    )


@dataclass(frozen=True)
class Purchase:
    payment_id: int
    created_at: datetime
    product: str
    stars: int
    coins: int
    days: int | None
    status: str
    refund_reason: str | None
    refund_shortfall: int | None


async def purchases(session: AsyncSession, user_id: int) -> list[Purchase]:
    """An account's purchases, newest first, for admins."""
    rows = await session.execute(
        select(StarsPayment, StarsInvoice)
        .join(StarsInvoice, StarsInvoice.id == StarsPayment.invoice_id)
        .where(StarsPayment.user_id == user_id)
        .order_by(StarsPayment.created_at.desc(), StarsPayment.id.desc())
    )
    return [
        Purchase(
            int(p.id),
            p.created_at,
            i.product,
            p.stars,
            i.coins,
            i.days,
            p.status,
            p.refund_reason,
            p.refund_shortfall,
        )
        for p, i in rows
    ]
