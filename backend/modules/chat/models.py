"""Conversations between matched users (D-027).

PostgreSQL is the source of truth for who is talking to whom. A user can be in at
most one open conversation, which a partial unique index enforces even under
concurrent matching.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

# Why a conversation ended. More reasons are added with idle timeouts, blocking, and
# unreachable partners.
END_REASONS = ("user_ended",)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            f"end_reason IN ({', '.join(repr(r) for r in END_REASONS)})", name="end_reason"
        ),
        CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ended_pair"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(16))
    # Who ended it, when a user did. NULL for automatic endings.
    ended_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))


class ConversationParticipant(Base):
    """One row per user per conversation. `left_at` is set when the conversation ends."""

    __tablename__ = "conversation_participants"
    __table_args__ = (
        # At most one open conversation per user: the final guard against double matches.
        Index(
            "uq_conversation_participants_open_user",
            "user_id",
            unique=True,
            postgresql_where=text("left_at IS NULL"),
        ),
        # Serves "conversations this user was in", e.g. for repeat-match avoidance.
        Index("ix_conversation_participants_user_id", "user_id", "conversation_id"),
    )

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
