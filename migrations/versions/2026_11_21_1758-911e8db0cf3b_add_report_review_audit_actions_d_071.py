"""add report review audit actions (D-071)

Revision ID: 911e8db0cf3b
Revises: 9e5a677971f6
Create Date: 2026-11-21 17:58:01.163157+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "911e8db0cf3b"
down_revision: str | Sequence[str] | None = "9e5a677971f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_ACTIONS = (
    "'role_changed', 'setting_changed', 'coins_adjusted', 'purchase_refunded', "
    "'payment_recorded', 'report_closed', 'account_warned', 'account_suspended', "
    "'account_banned', 'restriction_lifted'"
)
NEW_ACTIONS = OLD_ACTIONS + ", 'report_viewed', 'media_viewed'"


def _actions(actions: str) -> None:
    op.drop_constraint(op.f("ck_admin_audit_log_action"), "admin_audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_audit_log_action"), "admin_audit_log", f"action IN ({actions})"
    )


def upgrade() -> None:
    _actions(NEW_ACTIONS)


def downgrade() -> None:
    # The audit log is append-only: this fails while any review entry exists, rather
    # than deleting audit history.
    _actions(OLD_ACTIONS)
