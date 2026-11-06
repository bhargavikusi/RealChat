"""add notification settings (D-052)

Revision ID: bdfaf8d14f37
Revises: de4e30ce643d
Create Date: 2026-11-06 05:46:46.772315+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bdfaf8d14f37"
down_revision: str | Sequence[str] | None = "de4e30ce643d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_settings",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("silent_mode", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("visit_alerts", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("like_alerts", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("reminders", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_notification_settings")),
    )


def downgrade() -> None:
    op.drop_table("notification_settings")
