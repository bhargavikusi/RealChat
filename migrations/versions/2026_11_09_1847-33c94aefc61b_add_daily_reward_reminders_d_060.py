"""add daily reward reminders (D-060)

Revision ID: 33c94aefc61b
Revises: cc44ac905ad0
Create Date: 2026-11-09 18:47:17.638231+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "33c94aefc61b"
down_revision: str | Sequence[str] | None = "cc44ac905ad0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_reminders",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="sending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'unreachable', 'failed')",
            name=op.f("ck_daily_reminders_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_daily_reminders_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "day", name=op.f("pk_daily_reminders")),
    )
    op.create_index("ix_daily_reminders_day", "daily_reminders", ["day"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_daily_reminders_day", table_name="daily_reminders")
    op.drop_table("daily_reminders")
