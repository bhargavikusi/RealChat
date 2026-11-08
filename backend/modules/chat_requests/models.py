"""Chat requests (D-058). PostgreSQL is the source of truth."""

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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

# pending: the price is held and the notice is on its way. delivered: Telegram accepted
# the notice, the price was taken, and the recipient can answer. accepted: the
# recipient accepted while the sender was busy, who is asked to switch. connected: the
# two are chatting. declined, expired (not answered in time), lapsed (the busy sender
# stayed or didn't answer), closed (the sender connected through another request, or
# someone was no longer available when it was accepted), failed (the notice could not
# be delivered, and the hold was released).
REQUEST_STATUSES = (
    "pending",
    "delivered",
    "accepted",
    "connected",
    "declined",
    "expired",
    "lapsed",
    "closed",
    "failed",
)
# Still waiting for someone: a sender has at most one of these per recipient.
WAITING_STATUSES = ("pending", "delivered", "accepted")
FAILURE_REASONS = ("unreachable", "error", "interrupted")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ChatRequest(Base):
    """One request from a sender to start a chat with a recipient."""

    __tablename__ = "chat_requests"
    __table_args__ = (
        CheckConstraint("sender_user_id <> recipient_user_id", name="not_self"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        CheckConstraint(
            f"failure_reason IS NULL OR {_in('failure_reason', FAILURE_REASONS)}",
            name="failure_reason",
        ),
        CheckConstraint("(status = 'failed') = (failure_reason IS NOT NULL)", name="failed_pair"),
        CheckConstraint("price > 0", name="price"),
        CheckConstraint(
            "(status IN ('pending', 'failed')) = (delivered_at IS NULL)", name="delivered_pair"
        ),
        # Accepted while the sender was busy: the time to switch counts from then.
        CheckConstraint("status <> 'accepted' OR accepted_at IS NOT NULL", name="accepted_at"),
        CheckConstraint(
            f"({_in('status', WAITING_STATUSES)}) = (settled_at IS NULL)", name="settled_pair"
        ),
        CheckConstraint("(status = 'connected') = (conversation_id IS NOT NULL)", name="connected"),
        # One waiting request per sender and recipient.
        Index(
            "uq_chat_requests_waiting_pair",
            "sender_user_id",
            "recipient_user_id",
            unique=True,
            postgresql_where=text(_in("status", WAITING_STATUSES)),
        ),
        # A hold pays for one request, and a conversation comes from one request.
        Index("uq_chat_requests_hold_id", "hold_id", unique=True),
        Index(
            "uq_chat_requests_conversation_id",
            "conversation_id",
            unique=True,
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
        # The worker's jobs: interrupted sends, expiry, and busy senders' time to switch.
        Index(
            "ix_chat_requests_pending_created_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_chat_requests_delivered_at",
            "delivered_at",
            postgresql_where=text("status = 'delivered'"),
        ),
        Index(
            "ix_chat_requests_accepted_at",
            "accepted_at",
            postgresql_where=text("status = 'accepted'"),
        ),
        Index("ix_chat_requests_recipient_user_id", "recipient_user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sender_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    recipient_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    # One request per Send: a repeated or retried Send finds the same row.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    price: Mapped[int] = mapped_column(Integer)
    hold_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("coin_holds.id"))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    failure_reason: Mapped[str | None] = mapped_column(String(16))
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When it stopped waiting: connected, declined, expired, lapsed, closed, or failed.
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
