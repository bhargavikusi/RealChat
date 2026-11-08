"""add chat requests (D-058)

Revision ID: 4576e3cdfc41
Revises: 6ebdeaca4fc9
Create Date: 2026-11-08 19:46:46.192392+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4576e3cdfc41"
down_revision: str | Sequence[str] | None = "6ebdeaca4fc9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
    "'admin_grant', 'admin_correction', 'account_deleted', 'direct_message'"
)
NEW_KINDS = OLD_KINDS + ", 'chat_request'"
OLD_PURPOSES = "'paid_match', 'direct_message'"
NEW_PURPOSES = OLD_PURPOSES + ", 'chat_request'"


def _coin_checks(kinds: str, purposes: str) -> None:
    """Replace the coin checks that list ledger kinds and hold purposes."""
    for name, table, condition in (
        ("ck_coin_transactions_kind", "coin_transactions", f"kind IN ({kinds})"),
        (
            "ck_coin_transactions_hold_kind",
            "coin_transactions",
            f"hold_id IS NULL OR kind IN ({purposes})",
        ),
        ("ck_coin_holds_purpose", "coin_holds", f"purpose IN ({purposes})"),
    ):
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    _coin_checks(NEW_KINDS, NEW_PURPOSES)
    op.create_table(
        "chat_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("sender_user_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("hold_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("failure_reason", sa.String(length=16), nullable=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'connected') = (conversation_id IS NOT NULL)",
            name=op.f("ck_chat_requests_connected"),
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name=op.f("ck_chat_requests_failed_pair"),
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'delivered', 'accepted')) = (settled_at IS NULL)",
            name=op.f("ck_chat_requests_settled_pair"),
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'failed')) = (delivered_at IS NULL)",
            name=op.f("ck_chat_requests_delivered_pair"),
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('unreachable', 'error', 'interrupted')",
            name=op.f("ck_chat_requests_failure_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'accepted' OR accepted_at IS NOT NULL",
            name=op.f("ck_chat_requests_accepted_at"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'accepted', 'connected', 'declined', "
            "'expired', 'lapsed', 'closed', 'failed')",
            name=op.f("ck_chat_requests_status"),
        ),
        sa.CheckConstraint("price > 0", name=op.f("ck_chat_requests_price")),
        sa.CheckConstraint(
            "sender_user_id <> recipient_user_id", name=op.f("ck_chat_requests_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_chat_requests_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["hold_id"], ["coin_holds.id"], name=op.f("fk_chat_requests_hold_id_coin_holds")
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            name=op.f("fk_chat_requests_recipient_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"],
            ["users.id"],
            name=op.f("fk_chat_requests_sender_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_chat_requests_idempotency_key")),
    )
    op.create_index(
        "ix_chat_requests_accepted_at",
        "chat_requests",
        ["accepted_at"],
        unique=False,
        postgresql_where=sa.text("status = 'accepted'"),
    )
    op.create_index(
        "ix_chat_requests_delivered_at",
        "chat_requests",
        ["delivered_at"],
        unique=False,
        postgresql_where=sa.text("status = 'delivered'"),
    )
    op.create_index(
        "ix_chat_requests_pending_created_at",
        "chat_requests",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_chat_requests_recipient_user_id", "chat_requests", ["recipient_user_id"], unique=False
    )
    op.create_index(
        "uq_chat_requests_conversation_id",
        "chat_requests",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )
    op.create_index("uq_chat_requests_hold_id", "chat_requests", ["hold_id"], unique=True)
    op.create_index(
        "uq_chat_requests_waiting_pair",
        "chat_requests",
        ["sender_user_id", "recipient_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'delivered', 'accepted')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_chat_requests_waiting_pair",
        table_name="chat_requests",
        postgresql_where=sa.text("status IN ('pending', 'delivered', 'accepted')"),
    )
    op.drop_index("uq_chat_requests_hold_id", table_name="chat_requests")
    op.drop_index(
        "uq_chat_requests_conversation_id",
        table_name="chat_requests",
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )
    op.drop_index("ix_chat_requests_recipient_user_id", table_name="chat_requests")
    op.drop_index(
        "ix_chat_requests_pending_created_at",
        table_name="chat_requests",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        "ix_chat_requests_delivered_at",
        table_name="chat_requests",
        postgresql_where=sa.text("status = 'delivered'"),
    )
    op.drop_index(
        "ix_chat_requests_accepted_at",
        table_name="chat_requests",
        postgresql_where=sa.text("status = 'accepted'"),
    )
    op.drop_table("chat_requests")
    # Ledger entries and holds are never deleted, so this fails while any
    # `chat_request` ledger entry or hold exists, rather than rewriting history.
    _coin_checks(OLD_KINDS, OLD_PURPOSES)
