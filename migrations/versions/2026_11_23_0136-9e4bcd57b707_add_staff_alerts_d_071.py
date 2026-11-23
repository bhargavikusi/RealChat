"""add staff alerts (D-071)

Revision ID: 9e4bcd57b707
Revises: 50be4576b2e1
Create Date: 2026-11-23 01:36:21.449220+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e4bcd57b707"
down_revision: str | Sequence[str] | None = "50be4576b2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "staff_alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("recipient_user_id", sa.BigInteger(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=True),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'summary') = (day IS NOT NULL)", name=op.f("ck_staff_alerts_day_pair")
        ),
        sa.CheckConstraint(
            "(kind = 'urgent') = (report_id IS NOT NULL)", name=op.f("ck_staff_alerts_report_pair")
        ),
        sa.CheckConstraint("kind IN ('urgent', 'summary')", name=op.f("ck_staff_alerts_kind")),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            name=op.f("fk_staff_alerts_recipient_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["reports.id"], name=op.f("fk_staff_alerts_report_id_reports")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_alerts")),
    )
    op.create_index(
        "ix_staff_alerts_due",
        "staff_alerts",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_index(
        "uq_staff_alerts_summary",
        "staff_alerts",
        ["day", "recipient_user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'summary'"),
    )
    op.create_index(
        "uq_staff_alerts_urgent",
        "staff_alerts",
        ["report_id", "recipient_user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'urgent'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_staff_alerts_urgent",
        table_name="staff_alerts",
        postgresql_where=sa.text("kind = 'urgent'"),
    )
    op.drop_index(
        "uq_staff_alerts_summary",
        table_name="staff_alerts",
        postgresql_where=sa.text("kind = 'summary'"),
    )
    op.drop_index(
        "ix_staff_alerts_due",
        table_name="staff_alerts",
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.drop_table("staff_alerts")
