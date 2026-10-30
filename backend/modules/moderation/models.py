"""Blocks and reports between chat partners (D-037, D-045).

Both are kept when either account is deleted: they hold no profile data, and keeping
them stops a deletion from being used to undo a block or erase a report.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

# Report reasons (D-037), in the order they are offered. "under_18" is urgent.
REPORT_REASONS = ("spam", "harassment", "sexual", "under_18", "threats", "other")
URGENT_REASONS = ("under_18",)
REPORT_STATUSES = ("open", "closed")
REPORT_DETAILS_MAX_LENGTH = 500


class UserBlock(Base):
    """One block of one user by another. Unblocking sets `unblocked_at`; the row stays.

    Blocks do not affect random matching (D-045). They will stop direct messages and
    chat requests once those exist.
    """

    __tablename__ = "user_blocks"
    __table_args__ = (
        CheckConstraint("blocker_user_id <> blocked_user_id", name="not_self"),
        # At most one block in force per pair: repeated or concurrent presses add none.
        Index(
            "uq_user_blocks_active_pair",
            "blocker_user_id",
            "blocked_user_id",
            unique=True,
            postgresql_where=text("unblocked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    blocker_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    blocked_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # The conversation the block was made from: a user can only block a past partner.
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    unblocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(Base):
    """A report about a chat partner. The conversation's messages are kept while it is open."""

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(f"reason IN ({', '.join(repr(r) for r in REPORT_REASONS)})", name="reason"),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in REPORT_STATUSES)})", name="status"
        ),
        CheckConstraint("(status = 'closed') = (closed_at IS NOT NULL)", name="closed_pair"),
        CheckConstraint("reporter_user_id <> reported_user_id", name="not_self"),
        CheckConstraint(
            f"char_length(details) BETWEEN 1 AND {REPORT_DETAILS_MAX_LENGTH}", name="details"
        ),
        # One report per user per conversation: repeated or stale presses add none.
        UniqueConstraint("reporter_user_id", "conversation_id"),
        # Serves the per-user limit on reports in a period.
        Index("ix_reports_reporter_created_at", "reporter_user_id", "created_at"),
        # Serves "is this conversation under an open report" for retention cleanup.
        Index(
            "ix_reports_open_conversation_id",
            "conversation_id",
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    reporter_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    reported_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    reason: Mapped[str] = mapped_column(String(16))
    # Optional context from the reporter, added after the reason. For moderators only.
    details: Mapped[str | None] = mapped_column(String(REPORT_DETAILS_MAX_LENGTH))
    urgent: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
