"""Daily reward reminders (D-060). PostgreSQL is the source of truth."""

from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

# sending: recorded before the message goes out, so a restart never sends it twice.
# sent, unreachable (the user blocked the bot or is gone), or failed (any other error).
REMINDER_STATUSES = ("sending", "sent", "unreachable", "failed")


class DailyReminder(Base):
    """One daily reward reminder: at most one per user per UTC day."""

    __tablename__ = "daily_reminders"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in REMINDER_STATUSES)})", name="status"
        ),
        # The worker's cleanup deletes old rows by day.
        Index("ix_daily_reminders_day", "day"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)  # the UTC day reminded about
    status: Mapped[str] = mapped_column(String(16), server_default="sending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
