"""add direct messages (D-057)

Revision ID: 6ebdeaca4fc9
Revises: 95babc71da96
Create Date: 2026-11-07 19:56:00.420516+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "6ebdeaca4fc9"
down_revision: str | Sequence[str] | None = "95babc71da96"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
    "'admin_grant', 'admin_correction', 'account_deleted'"
)
NEW_KINDS = OLD_KINDS + ", 'direct_message'"
OLD_PURPOSES = "'paid_match'"
NEW_PURPOSES = "'paid_match', 'direct_message'"


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


HOLD_FUNCTION = """
CREATE OR REPLACE FUNCTION coin_transactions_hold_matches() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    hold RECORD;
BEGIN
    SELECT user_id, amount, status, purpose INTO hold FROM coin_holds WHERE id = NEW.hold_id;
    IF hold.status IS DISTINCT FROM 'captured'
        OR hold.user_id <> NEW.user_id
        OR NEW.amount <> -hold.amount
        OR hold.purpose IS DISTINCT FROM NEW.kind
    THEN
        RAISE EXCEPTION 'a ledger entry does not match its coin hold'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""
# As created with the ledger, before there was more than one hold purpose.
OLD_HOLD_FUNCTION = """
CREATE OR REPLACE FUNCTION coin_transactions_hold_matches() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    hold RECORD;
BEGIN
    SELECT user_id, amount, status INTO hold FROM coin_holds WHERE id = NEW.hold_id;
    IF hold.status IS DISTINCT FROM 'captured'
        OR hold.user_id <> NEW.user_id
        OR NEW.amount <> -hold.amount
    THEN
        RAISE EXCEPTION 'a ledger entry does not match its coin hold'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    _coin_checks(NEW_KINDS, NEW_PURPOSES)
    # With two hold purposes, a ledger entry must also be of its hold's purpose.
    op.execute(HOLD_FUNCTION)
    op.create_table(
        "direct_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("sender_user_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reply_to_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("file_id", sa.String(length=256), nullable=True),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("hold_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("failure_reason", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name=op.f("ck_direct_messages_failed_pair"),
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('unreachable', 'error', 'interrupted')",
            name=op.f("ck_direct_messages_failure_reason"),
        ),
        sa.CheckConstraint(
            "kind IN ('text', 'photo', 'video', 'animation', 'audio', 'voice', 'video_note', "
            "'sticker', 'document')",
            name=op.f("ck_direct_messages_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'viewed')",
            name=op.f("ck_direct_messages_status"),
        ),
        sa.CheckConstraint(
            "(price > 0) = (hold_id IS NOT NULL)", name=op.f("ck_direct_messages_hold_pair")
        ),
        sa.CheckConstraint("price >= 0", name=op.f("ck_direct_messages_price")),
        sa.CheckConstraint(
            "reply_to_id IS NULL OR price = 0", name=op.f("ck_direct_messages_free_reply")
        ),
        sa.CheckConstraint(
            "(kind = 'text') = (file_id IS NULL)", name=op.f("ck_direct_messages_content_file")
        ),
        sa.CheckConstraint(
            "kind <> 'text' OR text IS NOT NULL", name=op.f("ck_direct_messages_content_text")
        ),
        sa.CheckConstraint(
            "(status IN ('delivered', 'viewed')) = (delivered_at IS NOT NULL)",
            name=op.f("ck_direct_messages_delivered_pair"),
        ),
        sa.CheckConstraint(
            "(status = 'viewed') = (viewed_at IS NOT NULL)",
            name=op.f("ck_direct_messages_viewed_pair"),
        ),
        sa.CheckConstraint(
            "sender_user_id <> recipient_user_id", name=op.f("ck_direct_messages_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["hold_id"], ["coin_holds.id"], name=op.f("fk_direct_messages_hold_id_coin_holds")
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            name=op.f("fk_direct_messages_recipient_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reply_to_id"],
            ["direct_messages.id"],
            name=op.f("fk_direct_messages_reply_to_id_direct_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"],
            ["users.id"],
            name=op.f("fk_direct_messages_sender_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_direct_messages")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_direct_messages_idempotency_key")),
    )
    op.create_index(
        "ix_direct_messages_created_at", "direct_messages", ["created_at"], unique=False
    )
    op.create_index(
        "ix_direct_messages_pending_created_at",
        "direct_messages",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_direct_messages_recipient_user_id",
        "direct_messages",
        ["recipient_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_direct_messages_sender_user_id", "direct_messages", ["sender_user_id"], unique=False
    )
    op.create_index(
        "uq_direct_messages_hold_id",
        "direct_messages",
        ["hold_id"],
        unique=True,
        postgresql_where=sa.text("hold_id IS NOT NULL"),
    )
    op.create_index(
        "uq_direct_messages_reply_to_id",
        "direct_messages",
        ["reply_to_id"],
        unique=True,
        postgresql_where=sa.text("reply_to_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_direct_messages_reply_to_id",
        table_name="direct_messages",
        postgresql_where=sa.text("reply_to_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_direct_messages_hold_id",
        table_name="direct_messages",
        postgresql_where=sa.text("hold_id IS NOT NULL"),
    )
    op.drop_index("ix_direct_messages_sender_user_id", table_name="direct_messages")
    op.drop_index("ix_direct_messages_recipient_user_id", table_name="direct_messages")
    op.drop_index(
        "ix_direct_messages_pending_created_at",
        table_name="direct_messages",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_direct_messages_created_at", table_name="direct_messages")
    op.drop_table("direct_messages")
    # Ledger entries and holds are never deleted, so this fails while any
    # `direct_message` ledger entry or hold exists (released ones included), rather than
    # rewriting history.
    _coin_checks(OLD_KINDS, OLD_PURPOSES)
    op.execute(OLD_HOLD_FUNCTION)
