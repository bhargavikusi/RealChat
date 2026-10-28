"""Runtime settings and the admin audit log (D-034, D-035)."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

AUDIT_ACTIONS = ("role_changed", "setting_changed")


class AppSetting(Base):
    """A runtime setting changed by an admin. A missing row means the default applies.

    Keys and allowed ranges are defined in `backend.modules.admin.settings`, so a new
    setting needs no migration. Every change is also written to the audit log.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminAuditEntry(Base):
    """Append-only record of administrative changes. Rows are never updated or deleted."""

    __tablename__ = "admin_audit_log"
    __table_args__ = (
        CheckConstraint(f"action IN ({', '.join(repr(a) for a in AUDIT_ACTIONS)})", name="action"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # NULL means the server-side admin CLI, which has no RealChat account.
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(32))
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    setting_key: Mapped[str | None] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(String(64))
    new_value: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
