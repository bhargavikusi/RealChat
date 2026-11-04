"""Coin balances, credits, spending, and holds (requirements §16, D-050).

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids are internal `users.id` values.

Rules kept here:
* A balance is the sum of the user's ledger entries; nothing else changes it.
* Every entry and hold has an idempotency key: repeating an action with the same key
  records nothing more and reports what was recorded the first time.
* Spending and holding never take a balance below what is held: the user's row is
  locked (FOR NO KEY UPDATE) while available coins are counted, so simultaneous spends
  are counted one after the other.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.coins.models import TRANSACTION_KINDS, CoinHold, CoinTransaction
from backend.modules.users.models import User


class InsufficientCoinsError(Exception):
    """Not enough available coins for a spend or hold."""

    def __init__(self, available: int, needed: int) -> None:
        super().__init__(f"{available} coins available, {needed} needed")
        self.available = available
        self.needed = needed


@dataclass(frozen=True)
class Balance:
    total: int  # the sum of the ledger
    held: int  # set aside for searches still running

    @property
    def available(self) -> int:
        return self.total - self.held


async def balance(session: AsyncSession, user_id: int) -> Balance:
    total = await session.scalar(
        select(func.coalesce(func.sum(CoinTransaction.amount), 0)).where(
            CoinTransaction.user_id == user_id
        )
    )
    held = await session.scalar(
        select(func.coalesce(func.sum(CoinHold.amount), 0)).where(
            CoinHold.user_id == user_id, CoinHold.status == "held"
        )
    )
    return Balance(int(total or 0), int(held or 0))


async def _lock_user(session: AsyncSession, user_id: int) -> None:
    """Serialize this user's spending. FOR NO KEY UPDATE does not conflict with the
    key-share locks other rows' foreign keys take on the user."""
    await session.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))


async def _record(
    session: AsyncSession,
    user_id: int,
    amount: int,
    kind: str,
    key: str,
    *,
    hold_id: int | None = None,
    note: str | None = None,
) -> bool:
    """Add a ledger entry unless one with `key` exists; returns whether it was added."""
    if kind not in TRANSACTION_KINDS:
        raise ValueError(f"unknown coin transaction kind: {kind}")
    if amount == 0:
        raise ValueError("a coin transaction must change the balance")
    added = await session.scalar(
        insert(CoinTransaction)
        .values(
            user_id=user_id,
            amount=amount,
            kind=kind,
            idempotency_key=key,
            hold_id=hold_id,
            note=note,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(CoinTransaction.id)
    )
    return added is not None


async def credit(
    session: AsyncSession,
    user_id: int,
    amount: int,
    kind: str,
    key: str,
    *,
    note: str | None = None,
) -> bool:
    """Add coins; returns False if `key` was already recorded."""
    if amount <= 0:
        raise ValueError("a credit must be positive")
    return await _record(session, user_id, amount, kind, key, note=note)


async def debit(
    session: AsyncSession,
    user_id: int,
    amount: int,
    kind: str,
    key: str,
    *,
    note: str | None = None,
) -> bool:
    """Remove coins; returns False if `key` was already recorded.

    Raises InsufficientCoinsError if fewer than `amount` coins are available.
    """
    if amount <= 0:
        raise ValueError("a debit amount must be positive")
    await _lock_user(session, user_id)
    if await _key_recorded(session, key):
        return False
    available = (await balance(session, user_id)).available
    if available < amount:
        raise InsufficientCoinsError(available, amount)
    return await _record(session, user_id, -amount, kind, key, note=note)


async def _key_recorded(session: AsyncSession, key: str) -> bool:
    found = await session.scalar(
        select(CoinTransaction.id).where(CoinTransaction.idempotency_key == key)
    )
    return found is not None


async def hold(session: AsyncSession, user_id: int, amount: int, purpose: str, key: str) -> int:
    """Set coins aside; returns the hold's id (the existing one if `key` was used).

    Raises InsufficientCoinsError if fewer than `amount` coins are available.
    """
    if amount <= 0:
        raise ValueError("a hold must be positive")
    await _lock_user(session, user_id)
    existing = await session.scalar(select(CoinHold.id).where(CoinHold.idempotency_key == key))
    if existing is not None:
        return int(existing)
    available = (await balance(session, user_id)).available
    if available < amount:
        raise InsufficientCoinsError(available, amount)
    hold_id = (
        await session.execute(
            insert(CoinHold)
            .values(user_id=user_id, amount=amount, purpose=purpose, idempotency_key=key)
            .returning(CoinHold.id)
        )
    ).scalar_one()
    return int(hold_id)


async def capture(session: AsyncSession, hold_id: int, kind: str, now: datetime) -> bool:
    """Spend held coins; returns whether this call captured it (False if it had already
    been captured or released). The status change and the ledger entry are written
    together, and the entry's key is tied to the hold, so a hold is spent at most once."""
    captured = (
        await session.execute(
            update(CoinHold)
            .where(CoinHold.id == hold_id, CoinHold.status == "held")
            .values(status="captured", settled_at=now)
            .returning(CoinHold.user_id, CoinHold.amount)
        )
    ).first()
    if captured is None:
        return False
    user_id, amount = captured
    await _record(session, user_id, -amount, kind, f"hold:{hold_id}:capture", hold_id=hold_id)
    return True


async def release(session: AsyncSession, hold_id: int, now: datetime) -> bool:
    """Give held coins back; returns whether this call released it."""
    released = await session.scalar(
        update(CoinHold)
        .where(CoinHold.id == hold_id, CoinHold.status == "held")
        .values(status="released", settled_at=now)
        .returning(CoinHold.id)
    )
    return released is not None


@dataclass(frozen=True)
class Entry:
    amount: int
    kind: str
    created_at: datetime


async def history(session: AsyncSession, user_id: int, limit: int) -> list[Entry]:
    """The user's most recent ledger entries, newest first."""
    rows = await session.execute(
        select(CoinTransaction.amount, CoinTransaction.kind, CoinTransaction.created_at)
        .where(CoinTransaction.user_id == user_id)
        .order_by(CoinTransaction.id.desc())
        .limit(limit)
    )
    return [Entry(int(amount), kind, created_at) for amount, kind, created_at in rows]
