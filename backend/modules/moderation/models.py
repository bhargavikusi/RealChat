"""Blocks and reports between users who have met (D-037, D-045, D-054), and what
moderators do about them (D-071).

All are kept when either account is deleted: they hold no profile data, and keeping
them stops a deletion from being used to undo a block, erase a report, or hide a past
restriction.
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
    SmallInteger,
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
# How a closed report ended: an action on the account followed, or none did (D-071).
REPORT_OUTCOMES = ("no_action", "action_taken")
REPORT_DETAILS_MAX_LENGTH = 500

# What a moderator can do (D-071). "close" closes a report with no action on the account.
ACTION_KINDS = ("close", "warn", "suspend", "ban", "lift")
# The rules from the User Agreement (section 6.2) an action can name, in display order.
RULES = (
    "spam",
    "harassment",
    "sexual",
    "under_18",
    "threats",
    "impersonation",
    "private_info",
    "evasion",
    "other",
)
SUSPENSION_DAYS = (1, 3, 7, 30)
# Who a moderation notice goes to: the account acted on, the reporter, or the partner
# in a chat that the action ended.
NOTICE_KINDS = ("action", "reporter", "chat_ended")


def _in(column: str, values: tuple[str | int, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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
    # The conversation the block was made from, or NULL for someone the user only met
    # through a profile visit (D-054).
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    unblocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(Base):
    """A report about a chat partner, or about the profile of someone the user met only
    through a visit (D-054). A chat's messages are kept while its report is open."""

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(f"reason IN ({', '.join(repr(r) for r in REPORT_REASONS)})", name="reason"),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in REPORT_STATUSES)})", name="status"
        ),
        CheckConstraint("(status = 'closed') = (closed_at IS NOT NULL)", name="closed_pair"),
        CheckConstraint(_in("outcome", REPORT_OUTCOMES), name="outcome"),
        CheckConstraint("(status = 'closed') = (outcome IS NOT NULL)", name="outcome_pair"),
        CheckConstraint("reporter_user_id <> reported_user_id", name="not_self"),
        CheckConstraint(
            f"char_length(details) BETWEEN 1 AND {REPORT_DETAILS_MAX_LENGTH}", name="details"
        ),
        # One report per user per conversation: repeated or stale presses add none.
        UniqueConstraint("reporter_user_id", "conversation_id"),
        # And one profile report (no conversation) per user per reported user.
        Index(
            "uq_reports_profile_pair",
            "reporter_user_id",
            "reported_user_id",
            unique=True,
            postgresql_where=text("conversation_id IS NULL"),
        ),
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
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    reason: Mapped[str] = mapped_column(String(16))
    # Optional context from the reporter, added after the reason. For moderators only.
    details: Mapped[str | None] = mapped_column(String(REPORT_DETAILS_MAX_LENGTH))
    urgent: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(16))
    # The moderation action that closed it (D-071).
    closed_by_action_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("moderation_actions.id", use_alter=True)
    )


class ModerationAction(Base):
    """One moderation action (D-071). Rows are never updated: the account's current
    status lives on `users`, and this is its history."""

    __tablename__ = "moderation_actions"
    __table_args__ = (
        CheckConstraint(_in("kind", ACTION_KINDS), name="kind"),
        CheckConstraint(_in("rule", RULES), name="rule"),
        CheckConstraint(_in("days", SUSPENSION_DAYS), name="days"),
        CheckConstraint("actor_user_id <> target_user_id", name="not_self"),
        # Warnings, suspensions, and bans name a rule; closing and lifting don't.
        CheckConstraint(
            "(kind IN ('warn', 'suspend', 'ban')) = (rule IS NOT NULL)", name="rule_pair"
        ),
        CheckConstraint("(kind = 'suspend') = (days IS NOT NULL)", name="days_pair"),
        CheckConstraint("(days IS NULL) = (ends_at IS NULL)", name="ends_pair"),
        CheckConstraint("kind <> 'close' OR report_id IS NOT NULL", name="close_needs_report"),
        Index("ix_moderation_actions_target_created_at", "target_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    target_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # The report it was taken from, or NULL for an action from an account lookup.
    report_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("reports.id"))
    kind: Mapped[str] = mapped_column(String(16))
    rule: Mapped[str | None] = mapped_column(String(16))
    days: Mapped[int | None] = mapped_column(SmallInteger)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModerationNotice(Base):
    """A message an action owes someone, sent by the worker (D-071). The action is
    committed first, so a notice is never lost; a claim moves `next_attempt_at` ahead,
    so two workers never send the same notice at once."""

    __tablename__ = "moderation_notices"
    __table_args__ = (
        CheckConstraint(_in("kind", NOTICE_KINDS), name="kind"),
        CheckConstraint(
            "(kind = 'chat_ended') = (conversation_id IS NOT NULL)", name="conversation_pair"
        ),
        # One notice of each kind per action and person.
        UniqueConstraint("action_id", "kind", "recipient_user_id"),
        Index(
            "ix_moderation_notices_due",
            "next_attempt_at",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    action_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("moderation_actions.id"))
    kind: Mapped[str] = mapped_column(String(16))
    recipient_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # For "chat_ended": the conversation the action ended.
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
