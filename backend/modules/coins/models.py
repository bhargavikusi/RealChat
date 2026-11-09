"""The coin ledger and coin holds (requirements §16, D-050).

Every change to a user's coins is a row in `coin_transactions`, which is append-only:
database triggers reject updates, deletes, and truncation, and corrections are new,
compensating entries. A balance is the sum of a user's entries. Coins set aside for a
search, a direct message, or a chat request are a `coin_holds` row; capturing a hold
adds the spending entry, releasing it adds nothing. Both are kept when an account is
deleted (D-050).
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

# Why coins changed. Rewards and purchases add coins; spending removes them.
TRANSACTION_KINDS = (
    "welcome_bonus",
    "referral_reward",
    "daily_reward",
    "paid_match",
    "admin_grant",
    "admin_correction",
    # Coins removed because the account was deleted (D-055).
    "account_deleted",
    # A delivered direct message (D-057).
    "direct_message",
    # A delivered chat request (D-058).
    "chat_request",
    # An activity alert bought, and its refund when it never fires (D-059).
    "activity_alert",
    "activity_alert_refund",
)
# Kinds that add coins, and kinds that remove them; the database checks the sign.
CREDIT_KINDS = (
    "welcome_bonus",
    "referral_reward",
    "daily_reward",
    "admin_grant",
    "activity_alert_refund",
)
DEBIT_KINDS = (
    "paid_match",
    "admin_correction",
    "account_deleted",
    "direct_message",
    "chat_request",
    "activity_alert",
)
# Admin entries must say why.
ADMIN_KINDS = ("admin_grant", "admin_correction")
# Each is also the ledger kind of the entry that captures the hold.
HOLD_PURPOSES = ("paid_match", "direct_message", "chat_request")
HOLD_STATUSES = ("held", "captured", "released")


class CoinHold(Base):
    """Coins set aside for something that may still fail, such as a paid search."""

    __tablename__ = "coin_holds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="positive"),
        CheckConstraint(
            f"purpose IN ({', '.join(repr(p) for p in HOLD_PURPOSES)})", name="purpose"
        ),
        CheckConstraint(f"status IN ({', '.join(repr(s) for s in HOLD_STATUSES)})", name="status"),
        CheckConstraint("(status = 'held') = (settled_at IS NULL)", name="settled_pair"),
        # Serves "coins this user has on hold".
        Index("ix_coin_holds_held_user_id", "user_id", postgresql_where=text("status = 'held'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(32))
    # One hold per action: a retried or repeated action finds the same hold.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="held")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CoinTransaction(Base):
    """One change to a user's coins. Rows are never updated or deleted."""

    __tablename__ = "coin_transactions"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="nonzero"),
        CheckConstraint(
            f"(kind IN ({', '.join(repr(k) for k in CREDIT_KINDS)})) = (amount > 0)",
            name="sign",
        ),
        CheckConstraint(
            f"hold_id IS NULL OR kind IN ({', '.join(repr(p) for p in HOLD_PURPOSES)})",
            name="hold_kind",
        ),
        CheckConstraint(
            f"kind NOT IN ({', '.join(repr(k) for k in ADMIN_KINDS)}) OR note IS NOT NULL",
            name="admin_note",
        ),
        # A hold is spent by at most one entry.
        Index(
            "uq_coin_transactions_hold_id",
            "hold_id",
            unique=True,
            postgresql_where=text("hold_id IS NOT NULL"),
        ),
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in TRANSACTION_KINDS)})", name="kind"),
        # Serves balances and history.
        Index("ix_coin_transactions_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # Positive adds coins, negative removes them. Coins are whole numbers.
    amount: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    # One entry per action: a retried or duplicated action records once.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    # The hold this spending captured, if any.
    hold_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("coin_holds.id"))
    # Why an admin grant or correction was made. Never message content.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


REFERRAL_STATUSES = ("pending", "rewarded", "not_rewarded")


class InviteCode(Base):
    """A user's personal invite code, used in their invite link (D-050)."""

    __tablename__ = "invite_codes"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    # Random and unguessable; carries nothing about the user.
    code: Mapped[str] = mapped_column(String(16), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Referral(Base):
    """Who invited a user: at most one inviter per invited user, set when they start the
    bot through an invite link (D-050). The inviter is rewarded once the invited user
    completes their profile, unless the daily cap was reached or they can't be."""

    __tablename__ = "referrals"
    __table_args__ = (
        CheckConstraint("invitee_user_id <> inviter_user_id", name="not_self"),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in REFERRAL_STATUSES)})", name="status"
        ),
        CheckConstraint("(status = 'pending') = (settled_at IS NULL)", name="settled_pair"),
        # Serves the inviter's rewards per day.
        Index("ix_referrals_inviter_settled_at", "inviter_user_id", "settled_at"),
    )

    invitee_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), primary_key=True
    )
    inviter_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
