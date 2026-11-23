"""add users listed audit action (D-072)

Revision ID: f3a34bf5e5b5
Revises: 9e4bcd57b707
Create Date: 2026-11-23 21:11:02.944353+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a34bf5e5b5"
down_revision: str | Sequence[str] | None = "9e4bcd57b707"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_ACTIONS = (
    "'role_changed', 'setting_changed', 'coins_adjusted', 'purchase_refunded', "
    "'payment_recorded', 'report_closed', 'account_warned', 'account_suspended', "
    "'account_banned', 'restriction_lifted', 'report_viewed', 'media_viewed', "
    "'account_looked_up', 'account_viewed', 'chats_listed', 'chat_opened'"
)
NEW_ACTIONS = OLD_ACTIONS + ", 'users_listed'"


def _actions(actions: str) -> None:
    op.drop_constraint(op.f("ck_admin_audit_log_action"), "admin_audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_audit_log_action"), "admin_audit_log", f"action IN ({actions})"
    )


def upgrade() -> None:
    _actions(NEW_ACTIONS)


def downgrade() -> None:
    # The audit log is append-only: this fails while any users_listed entry exists,
    # rather than deleting audit history.
    _actions(OLD_ACTIONS)
