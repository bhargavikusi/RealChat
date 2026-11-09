"""Activity alerts (D-059). PostgreSQL is the source of truth."""

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

# active: bought and waiting for the person's next RealChat activity. fired: the buyer
# was alerted. refunded: the price was returned (see REFUND_REASONS). cancelled: the
# buyer cancelled it, without a refund.
ALERT_STATUSES = ("active", "fired", "refunded", "cancelled")
# expired: no activity within the period. blocked: a block either way. opted_out: the
# person turned activity alerts off. unavailable: the person's account can no longer
# use RealChat, or was deleted.
REFUND_REASONS = ("expired", "blocked", "opted_out", "unavailable")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ActivityAlert(Base):
    """One bought alert for the next time `target_user_id` uses RealChat."""

    __tablename__ = "activity_alerts"
    __table_args__ = (
        CheckConstraint("buyer_user_id <> target_user_id", name="not_self"),
        CheckConstraint(_in("status", ALERT_STATUSES), name="status"),
        CheckConstraint(
            f"refund_reason IS NULL OR {_in('refund_reason', REFUND_REASONS)}",
            name="refund_reason",
        ),
        CheckConstraint("(status = 'refunded') = (refund_reason IS NOT NULL)", name="refunded"),
        CheckConstraint("(status = 'active') = (settled_at IS NULL)", name="settled"),
        CheckConstraint("price > 0", name="price"),
        # One active alert per buyer and person.
        Index(
            "uq_activity_alerts_active_pair",
            "buyer_user_id",
            "target_user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        # The worker's job: active alerts, by person and by age.
        Index(
            "ix_activity_alerts_active_target",
            "target_user_id",
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_activity_alerts_active_created_at",
            "created_at",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    buyer_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    target_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    # One alert per Buy: a repeated or retried Buy finds the same row.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    price: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    refund_reason: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
