"""Users, profiles, and terms acceptances (PostgreSQL is the source of truth)."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base
from backend.modules.users.validation import (
    AGE_FLOOR,
    AGE_MAX,
    BIO_MAX_LENGTH,
    CITY_MAX_LENGTH,
    NICKNAME_MAX_LENGTH,
    NICKNAME_MIN_LENGTH,
    Gender,
    Visibility,
)

USER_STATUSES = ("active", "suspended", "banned", "deleted")
# Restricted accounts may only read help and delete their account (D-026).
RESTRICTED_STATUSES = ("suspended", "banned")
# Fields whose changes are limited by a cooldown and kept in the change history (D-019).
COOLDOWN_FIELDS = ("age", "gender")
# Access roles (D-034). Changed only by the server-side admin CLI, never by the bot.
ROLES = ("user", "moderator", "admin")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class User(Base):
    """One row per Telegram account. Kept (minimal) after account deletion (D-016)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in("status", USER_STATUSES), name="status"),
        CheckConstraint("status <> 'deleted' OR deleted_at IS NOT NULL", name="deleted_at_set"),
        CheckConstraint(_in("role", ROLES), name="role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Telegram's stable numeric ID. Usernames change and are never used as keys.
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    role: Mapped[str] = mapped_column(String(16), server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Profile(Base):
    """Public profile. Fields stay NULL until provided during onboarding."""

    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint(
            f"char_length(nickname) BETWEEN {NICKNAME_MIN_LENGTH} AND {NICKNAME_MAX_LENGTH}",
            name="nickname_length",
        ),
        CheckConstraint(f"age_at_entry BETWEEN {AGE_FLOOR} AND {AGE_MAX}", name="age_range"),
        CheckConstraint("(age_at_entry IS NULL) = (age_entered_on IS NULL)", name="age_pair"),
        CheckConstraint(_in("gender", tuple(g.value for g in Gender)), name="gender"),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="country_code_format"),
        CheckConstraint(f"char_length(city) BETWEEN 1 AND {CITY_MAX_LENGTH}", name="city_length"),
        CheckConstraint(f"char_length(bio) BETWEEN 1 AND {BIO_MAX_LENGTH}", name="bio_length"),
        # The service trims text; these stop blank or padded values written directly.
        CheckConstraint("nickname = btrim(nickname)", name="nickname_trimmed"),
        CheckConstraint("city = btrim(city)", name="city_trimmed"),
        CheckConstraint("bio = btrim(bio)", name="bio_trimmed"),
        CheckConstraint(_in("visibility", tuple(v.value for v in Visibility)), name="visibility"),
        CheckConstraint(
            "onboarding_completed_at IS NULL OR (nickname IS NOT NULL AND age_at_entry IS NOT NULL"
            " AND gender IS NOT NULL AND country_code IS NOT NULL)",
            name="complete_requires_fields",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    nickname: Mapped[str | None] = mapped_column(String(NICKNAME_MAX_LENGTH))
    # D-010: age as entered plus the date it was entered; current age is derived.
    age_at_entry: Mapped[int | None] = mapped_column(SmallInteger)
    age_entered_on: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(8))
    country_code: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(CITY_MAX_LENGTH))
    bio: Mapped[str | None] = mapped_column(String(BIO_MAX_LENGTH))
    # Telegram file identifier only; image files are never downloaded or stored.
    photo_file_id: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(8), server_default="visible")
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TermsAcceptance(Base):
    """Which terms version a user accepted, and when. One row per user and version."""

    __tablename__ = "terms_acceptances"
    __table_args__ = (UniqueConstraint("user_id", "terms_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # No separate index: the unique (user_id, terms_version) constraint serves lookups.
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    terms_version: Mapped[str] = mapped_column(String(32))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProfileChange(Base):
    """Append-only history of age and gender changes after onboarding (D-019, D-024).

    The latest row per field drives the change cooldown; the history is kept for
    moderation. Rows are never updated.
    """

    __tablename__ = "profile_changes"
    __table_args__ = (
        CheckConstraint(_in("field", COOLDOWN_FIELDS), name="field"),
        # Serves "latest change of this field for this user".
        Index("ix_profile_changes_user_field_changed_at", "user_id", "field", "changed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(16))
    # Age as the displayed (current) age at the time of the change; gender as stored.
    old_value: Mapped[str] = mapped_column(String(16))
    new_value: Mapped[str] = mapped_column(String(16))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
