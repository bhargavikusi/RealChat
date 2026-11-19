"""Telegram Stars invoices and payments (D-061, D-062). PostgreSQL is the source of truth.

An invoice row records exactly what was offered: its Stars price and what it buys (a
coin pack, or a VIP pass with its days and included coins).
The invoice sent to Telegram carries only a reference to that row, so checkout and
fulfilment always use what was stored. A payment row is written once per Telegram
charge, and is kept when an account is deleted, like the coin ledger.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

PRODUCTS = ("coins", "vip")
# paid: the Stars were received and the purchase fulfilled. refund_due: the purchase
# can't be fulfilled and the Stars must go back (see REFUND_REASONS); the worker tries
# again from `refund_retry_at` until Telegram accepts the refund. refunded: the Stars
# were returned. A trigger keeps rows from being deleted and lets the status only move
# forward (see the migration).
PAYMENT_STATUSES = ("paid", "refund_due", "refunded")
# account_deleted: the payment arrived after the account was deleted. admin: an admin
# refunded it with the server CLI, giving a note (D-065). telegram: Telegram reported it
# refunded, for example after a dispute.
REFUND_REASONS = ("account_deleted", "admin", "telegram")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class StarsInvoice(Base):
    """One invoice sent to a user, with what it offered."""

    __tablename__ = "stars_invoices"
    __table_args__ = (
        CheckConstraint(_in("product", PRODUCTS), name="product"),
        CheckConstraint("stars > 0", name="stars"),
        # A VIP pass may include no coins; a coin pack always has some.
        CheckConstraint("coins > 0 OR (product = 'vip' AND coins = 0)", name="coins"),
        CheckConstraint("(product = 'vip') = (days IS NOT NULL)", name="vip_days"),
        CheckConstraint("days IS NULL OR days > 0", name="days"),
        Index("ix_stars_invoices_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    product: Mapped[str] = mapped_column(String(16))
    stars: Mapped[int] = mapped_column(Integer)
    coins: Mapped[int] = mapped_column(Integer)
    # VIP days a pass adds (D-062); NULL for a coin pack.
    days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StarsPayment(Base):
    """One Telegram Stars payment, written once per Telegram charge."""

    __tablename__ = "stars_payments"
    __table_args__ = (
        CheckConstraint(_in("status", PAYMENT_STATUSES), name="status"),
        CheckConstraint(
            f"refund_reason IS NULL OR {_in('refund_reason', REFUND_REASONS)}",
            name="refund_reason",
        ),
        CheckConstraint("(status = 'paid') = (refund_reason IS NULL)", name="paid"),
        CheckConstraint("(status = 'refunded') = (refunded_at IS NOT NULL)", name="refunded"),
        CheckConstraint(
            "(status = 'refund_due') = (refund_retry_at IS NOT NULL)", name="refund_retry"
        ),
        CheckConstraint("stars > 0", name="stars"),
        CheckConstraint(
            "(refund_reason IS NOT DISTINCT FROM 'admin') = (refund_note IS NOT NULL)",
            name="refund_note",
        ),
        CheckConstraint("refund_shortfall IS NULL OR refund_shortfall > 0", name="shortfall"),
        Index("ix_stars_payments_user_id", "user_id"),
        Index(
            "ix_stars_payments_refund_retry_at",
            "refund_retry_at",
            postgresql_where=text("status = 'refund_due'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Telegram's identifier for the charge, needed to refund it.
    telegram_charge_id: Mapped[str] = mapped_column(String(255), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    invoice_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stars_invoices.id"))
    stars: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="paid")
    refund_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When a refund still owed may next be tried.
    refund_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # For a VIP pass that was granted: the VIP end date it produced (D-062).
    vip_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why an admin refunded it (D-065).
    refund_note: Mapped[str | None] = mapped_column(Text)
    # Coins a refund reported by Telegram couldn't take back, because they were spent
    # (D-065). NULL when none were missing.
    refund_shortfall: Mapped[int | None] = mapped_column(Integer)
