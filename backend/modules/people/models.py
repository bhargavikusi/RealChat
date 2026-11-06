"""Profile visits (D-053). PostgreSQL is the source of truth."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base


class ProfileVisit(Base):
    """The latest time one user opened another's profile card. One row per pair, so a
    visitor is listed once however often they visit; each visit moves it forward."""

    __tablename__ = "profile_visits"
    __table_args__ = (
        CheckConstraint("visitor_user_id <> visited_user_id", name="not_self"),
        # Visited you: one user's visitors, latest first.
        Index("ix_profile_visits_visited_latest", "visited_user_id", "last_visited_at"),
        # The retention job deletes by age.
        Index("ix_profile_visits_last_visited_at", "last_visited_at"),
    )

    visitor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    visited_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    last_visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
