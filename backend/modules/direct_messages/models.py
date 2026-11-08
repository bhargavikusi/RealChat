"""Direct messages (D-057). PostgreSQL is the source of truth."""

from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

# What can be sent: kinds whose content can be kept and sent again unchanged. Polls and
# dice can't be, so they are refused.
DIRECT_KINDS = (
    "text",
    "photo",
    "video",
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "document",
)
# pending: the coin is held and the notice is on its way. delivered: Telegram accepted
# the notice and the coin was taken. failed: the notice could not be delivered, and
# the hold was released. viewed: the recipient opened the message.
DIRECT_STATUSES = ("pending", "delivered", "failed", "viewed")
# Why a message failed: the recipient can't be reached (they blocked the bot or their
# account is gone), Telegram refused the notice for another reason, or sending was
# interrupted (found pending by the worker).
FAILURE_REASONS = ("unreachable", "error", "interrupted")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class DirectMessage(Base):
    """One direct message, or a free reply to one.

    The content is kept so the recipient can open it whatever the sender does with
    their copy. The whole row is deleted after the chat message retention period, or
    with either account.
    """

    __tablename__ = "direct_messages"
    __table_args__ = (
        CheckConstraint("sender_user_id <> recipient_user_id", name="not_self"),
        CheckConstraint(_in("kind", DIRECT_KINDS), name="kind"),
        CheckConstraint(_in("status", DIRECT_STATUSES), name="status"),
        CheckConstraint(
            f"failure_reason IS NULL OR {_in('failure_reason', FAILURE_REASONS)}",
            name="failure_reason",
        ),
        CheckConstraint("(status = 'failed') = (failure_reason IS NOT NULL)", name="failed_pair"),
        CheckConstraint("price >= 0", name="price"),
        # Paid messages are held; free replies are not.
        CheckConstraint("(price > 0) = (hold_id IS NOT NULL)", name="hold_pair"),
        # Only free replies point at a message.
        CheckConstraint("reply_to_id IS NULL OR price = 0", name="free_reply"),
        # Text has text and no file; every other kind has a file.
        CheckConstraint("(kind = 'text') = (file_id IS NULL)", name="content_file"),
        CheckConstraint("kind <> 'text' OR text IS NOT NULL", name="content_text"),
        # The times match the status.
        CheckConstraint(
            "(status IN ('delivered', 'viewed')) = (delivered_at IS NOT NULL)",
            name="delivered_pair",
        ),
        CheckConstraint("(status = 'viewed') = (viewed_at IS NOT NULL)", name="viewed_pair"),
        # A hold pays for one message.
        Index(
            "uq_direct_messages_hold_id",
            "hold_id",
            unique=True,
            postgresql_where=text("hold_id IS NOT NULL"),
        ),
        # One free reply per message.
        Index(
            "uq_direct_messages_reply_to_id",
            "reply_to_id",
            unique=True,
            postgresql_where=text("reply_to_id IS NOT NULL"),
        ),
        # The worker's sweep for messages left pending.
        Index(
            "ix_direct_messages_pending_created_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # Retention deletes by age; account deletion by either user.
        Index("ix_direct_messages_created_at", "created_at"),
        Index("ix_direct_messages_sender_user_id", "sender_user_id"),
        Index("ix_direct_messages_recipient_user_id", "recipient_user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sender_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    recipient_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    # The message this freely replies to. Cleared if that one is deleted first.
    reply_to_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("direct_messages.id", ondelete="SET NULL")
    )
    # One message per Send: a repeated or retried Send finds the same row.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)  # text or caption
    # Formatting of the text or caption, as Telegram message entities.
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    file_id: Mapped[str | None] = mapped_column(String(256))
    price: Mapped[int] = mapped_column(Integer)
    hold_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("coin_holds.id"))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    failure_reason: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
