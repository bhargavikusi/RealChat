"""add activity alerts (D-059)

Revision ID: cc44ac905ad0
Revises: 4576e3cdfc41
Create Date: 2026-11-09 00:40:38.045465+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cc44ac905ad0"
down_revision: str | Sequence[str] | None = "4576e3cdfc41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
    "'admin_grant', 'admin_correction', 'account_deleted', 'direct_message', "
    "'chat_request'"
)
NEW_KINDS = OLD_KINDS + ", 'activity_alert', 'activity_alert_refund'"
OLD_CREDITS = "'welcome_bonus', 'referral_reward', 'daily_reward', 'admin_grant'"
NEW_CREDITS = OLD_CREDITS + ", 'activity_alert_refund'"


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
        "activity_alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("buyer_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("refund_reason", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'active') = (settled_at IS NULL)", name=op.f("ck_activity_alerts_settled")
        ),
        sa.CheckConstraint(
            "(status = 'refunded') = (refund_reason IS NOT NULL)",
            name=op.f("ck_activity_alerts_refunded"),
        ),
        sa.CheckConstraint(
            "refund_reason IS NULL OR refund_reason IN "
            "('expired', 'blocked', 'opted_out', 'unavailable')",
            name=op.f("ck_activity_alerts_refund_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'fired', 'refunded', 'cancelled')",
            name=op.f("ck_activity_alerts_status"),
        ),
        sa.CheckConstraint(
            "buyer_user_id <> target_user_id", name=op.f("ck_activity_alerts_not_self")
        ),
        sa.CheckConstraint("price > 0", name=op.f("ck_activity_alerts_price")),
        sa.ForeignKeyConstraint(
            ["buyer_user_id"],
            ["users.id"],
            name=op.f("fk_activity_alerts_buyer_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["users.id"],
            name=op.f("fk_activity_alerts_target_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_alerts")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_activity_alerts_idempotency_key")),
    )
    op.create_index(
        "ix_activity_alerts_active_created_at",
        "activity_alerts",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_activity_alerts_active_target",
        "activity_alerts",
        ["target_user_id"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_activity_alerts_active_pair",
        "activity_alerts",
        ["buyer_user_id", "target_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.add_column(
        "notification_settings",
        sa.Column("activity_alerts", sa.Boolean(), server_default="true", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("notification_settings", "activity_alerts")
    op.drop_index(
        "uq_activity_alerts_active_pair",
        table_name="activity_alerts",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index(
        "ix_activity_alerts_active_target",
        table_name="activity_alerts",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index(
        "ix_activity_alerts_active_created_at",
        table_name="activity_alerts",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("activity_alerts")
    # The ledger is append-only, so this fails while any activity alert entry exists,
    # rather than rewriting history.
    _ledger_checks(OLD_KINDS, OLD_CREDITS)
