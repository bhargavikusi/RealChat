"""add stars invoices and payments (D-061)

Revision ID: 4c8f87d0b07f
Revises: 33c94aefc61b
Create Date: 2026-11-16 19:54:20.246061+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4c8f87d0b07f"
down_revision: str | Sequence[str] | None = "33c94aefc61b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
    "'admin_grant', 'admin_correction', 'account_deleted', 'direct_message', "
    "'chat_request', 'activity_alert', 'activity_alert_refund'"
)
NEW_KINDS = OLD_KINDS + ", 'coin_purchase'"
OLD_CREDITS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'admin_grant', 'activity_alert_refund'"
)
NEW_CREDITS = OLD_CREDITS + ", 'coin_purchase'"


def _ledger_checks(kinds: str, credits: str) -> None:
    """Replace the ledger checks that list kinds, and which kinds add coins."""
    for name, condition in (
        ("ck_coin_transactions_kind", f"kind IN ({kinds})"),
        ("ck_coin_transactions_sign", f"(kind IN ({credits})) = (amount > 0)"),
    ):
        op.drop_constraint(op.f(name), "coin_transactions", type_="check")
        op.create_check_constraint(op.f(name), "coin_transactions", condition)


def upgrade() -> None:
    _ledger_checks(NEW_KINDS, NEW_CREDITS)
    op.create_table(
        "stars_invoices",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("product", sa.String(length=16), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("coins", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("product IN ('coins')", name=op.f("ck_stars_invoices_product")),
        sa.CheckConstraint("coins > 0", name=op.f("ck_stars_invoices_coins")),
        sa.CheckConstraint("stars > 0", name=op.f("ck_stars_invoices_stars")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_stars_invoices_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stars_invoices")),
    )
    op.create_index("ix_stars_invoices_user_id", "stars_invoices", ["user_id"], unique=False)
    op.create_table(
        "stars_payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("telegram_charge_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="paid", nullable=False),
        sa.Column("refund_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'paid') = (refund_reason IS NULL)", name=op.f("ck_stars_payments_paid")
        ),
        sa.CheckConstraint(
            "(status = 'refunded') = (refunded_at IS NOT NULL)",
            name=op.f("ck_stars_payments_refunded"),
        ),
        sa.CheckConstraint(
            "refund_reason IS NULL OR refund_reason IN ('account_deleted')",
            name=op.f("ck_stars_payments_refund_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('paid', 'refund_due', 'refunded')", name=op.f("ck_stars_payments_status")
        ),
        sa.CheckConstraint("stars > 0", name=op.f("ck_stars_payments_stars")),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["stars_invoices.id"],
            name=op.f("fk_stars_payments_invoice_id_stars_invoices"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_stars_payments_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stars_payments")),
        sa.UniqueConstraint(
            "telegram_charge_id", name=op.f("uq_stars_payments_telegram_charge_id")
        ),
    )
    op.create_index("ix_stars_payments_user_id", "stars_payments", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_stars_payments_user_id", table_name="stars_payments")
    op.drop_table("stars_payments")
    op.drop_index("ix_stars_invoices_user_id", table_name="stars_invoices")
    op.drop_table("stars_invoices")
    # The ledger is append-only, so this fails while any purchase entry exists, rather
    # than rewriting history.
    _ledger_checks(OLD_KINDS, OLD_CREDITS)
