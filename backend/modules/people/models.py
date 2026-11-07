"""Profile visits (D-053) and likes (D-056). PostgreSQL is the source of truth."""

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


class ProfileLike(Base):
    """One user's like of another's profile. One row per pair: unliking clears
    `liked_at` but keeps the row, so `notified_at` still spaces out like notices when
    the user likes again (D-056)."""

    __tablename__ = "profile_likes"
    __table_args__ = (
        CheckConstraint("liker_user_id <> liked_user_id", name="not_self"),
        # Liked you: one user's likers, latest first.
        Index("ix_profile_likes_liked_latest", "liked_user_id", "liked_at"),
        # You liked: the profiles one user likes, latest first.
        Index("ix_profile_likes_liker_latest", "liker_user_id", "liked_at"),
    )

    liker_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    liked_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # When the current like was given; None once unliked.
    liked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the liked user was last sent a notice of this pair's like.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
