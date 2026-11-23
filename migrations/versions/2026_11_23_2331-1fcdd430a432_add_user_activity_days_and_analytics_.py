"""add user activity days and analytics audit (D-072)

Revision ID: 1fcdd430a432
Revises: 70d24136a469
Create Date: 2026-11-23 23:31:37.901645+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1fcdd430a432"
down_revision: str | Sequence[str] | None = "70d24136a469"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_ACTIONS = (
    "'role_changed', 'setting_changed', 'coins_adjusted', 'purchase_refunded', "
    "'payment_recorded', 'report_closed', 'account_warned', 'account_suspended', "
    "'account_banned', 'restriction_lifted', 'report_viewed', 'media_viewed', "
    "'account_looked_up', 'account_viewed', 'chats_listed', 'chat_opened', 'users_listed'"
)
NEW_ACTIONS = OLD_ACTIONS + ", 'analytics_viewed'"


def _actions(actions: str) -> None:
    op.drop_constraint(op.f("ck_admin_audit_log_action"), "admin_audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_audit_log_action"), "admin_audit_log", f"action IN ({actions})"
    )


def upgrade() -> None:
    op.create_table(
        "user_activity_days",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_activity_days_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "day", name=op.f("pk_user_activity_days")),
    )
    op.create_index("ix_user_activity_days_day", "user_activity_days", ["day"], unique=False)
    _actions(NEW_ACTIONS)


def downgrade() -> None:
    # The audit log is append-only: this fails while any analytics_viewed entry exists.
    _actions(OLD_ACTIONS)
    op.drop_index("ix_user_activity_days_day", table_name="user_activity_days")
    op.drop_table("user_activity_days")
