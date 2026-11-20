"""add recorded payment audit action (D-068)

Revision ID: c40b9ea815e5
Revises: 480c6b16c9bd
Create Date: 2026-11-20 00:58:34.129610+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c40b9ea815e5"
down_revision: str | Sequence[str] | None = "480c6b16c9bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_ACTIONS = "'role_changed', 'setting_changed', 'coins_adjusted', 'purchase_refunded'"
NEW_ACTIONS = OLD_ACTIONS + ", 'payment_recorded'"


def _actions(actions: str) -> None:
    op.drop_constraint(op.f("ck_admin_audit_log_action"), "admin_audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_audit_log_action"), "admin_audit_log", f"action IN ({actions})"
    )


def upgrade() -> None:
    _actions(NEW_ACTIONS)


def downgrade() -> None:
    # The audit log is append-only: this fails while any recorded payment entry exists,
    # rather than deleting audit history.
    _actions(OLD_ACTIONS)
