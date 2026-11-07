"""allow coin forfeiture on account deletion (D-055)

Revision ID: 2a724f8b7a48
Revises: 04f9a0c59ba3
Create Date: 2026-11-06 23:59:13.100132+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2a724f8b7a48"
down_revision: str | Sequence[str] | None = "04f9a0c59ba3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_KINDS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
    "'admin_grant', 'admin_correction'"
)
NEW_KINDS = OLD_KINDS + ", 'account_deleted'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_coin_transactions_kind"), "coin_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_coin_transactions_kind"), "coin_transactions", f"kind IN ({NEW_KINDS})"
    )


def downgrade() -> None:
    # The ledger is append-only, so forfeiture entries cannot be removed: the downgrade
    # fails while any exist, rather than rewriting history.
    op.drop_constraint(op.f("ck_coin_transactions_kind"), "coin_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_coin_transactions_kind"), "coin_transactions", f"kind IN ({OLD_KINDS})"
    )
