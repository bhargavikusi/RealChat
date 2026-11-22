"""add account lookup audit (D-071)

Revision ID: 50be4576b2e1
Revises: 911e8db0cf3b
Create Date: 2026-11-22 02:42:44.418527+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "50be4576b2e1"
down_revision: str | Sequence[str] | None = "911e8db0cf3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_ACTIONS = (
    "'role_changed', 'setting_changed', 'coins_adjusted', 'purchase_refunded', "
    "'payment_recorded', 'report_closed', 'account_warned', 'account_suspended', "
    "'account_banned', 'restriction_lifted', 'report_viewed', 'media_viewed'"
)
NEW_ACTIONS = OLD_ACTIONS + ", 'account_looked_up', 'chats_listed', 'chat_opened'"


def _actions(actions: str) -> None:
    op.drop_constraint(op.f("ck_admin_audit_log_action"), "admin_audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_audit_log_action"), "admin_audit_log", f"action IN ({actions})"
    )


def upgrade() -> None:
    op.add_column("admin_audit_log", sa.Column("reason", sa.String(length=300), nullable=True))
    _actions(NEW_ACTIONS)


def downgrade() -> None:
    # The audit log is append-only: restoring the old actions fails while any lookup
    # entry exists, so admins' reasons are never dropped with the column.
    _actions(OLD_ACTIONS)
    op.drop_column("admin_audit_log", "reason")
