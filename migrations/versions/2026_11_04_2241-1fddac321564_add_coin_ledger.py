"""add coin ledger

The append-only coin ledger and coin holds, and the "coins_adjusted" admin audit
action (D-050).

Revision ID: 1fddac321564
Revises: 2ee64b2df2f8
Create Date: 2026-11-04 22:41:10.945876+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1fddac321564"
down_revision: str | Sequence[str] | None = "2ee64b2df2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


HOLD_TRANSITION_FUNCTION = """
CREATE FUNCTION coin_holds_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'coin holds cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.status <> 'held'
        OR NEW.status NOT IN ('captured', 'released')
        OR (NEW.user_id, NEW.amount, NEW.purpose, NEW.idempotency_key, NEW.created_at)
            IS DISTINCT FROM
            (OLD.user_id, OLD.amount, OLD.purpose, OLD.idempotency_key, OLD.created_at)
    THEN
        RAISE EXCEPTION 'a coin hold can only go from held to captured or released'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

HOLD_ENTRY_FUNCTION = """
CREATE FUNCTION coin_transactions_hold_matches() RETURNS trigger LANGUAGE plpgsql AS $$
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

APPEND_ONLY_FUNCTION = """
CREATE FUNCTION coin_transactions_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'coin_transactions is append-only' USING ERRCODE = 'restrict_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "coin_holds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="held", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'held') = (settled_at IS NULL)", name=op.f("ck_coin_holds_settled_pair")
        ),
        sa.CheckConstraint("purpose IN ('paid_match')", name=op.f("ck_coin_holds_purpose")),
        sa.CheckConstraint(
            "status IN ('held', 'captured', 'released')", name=op.f("ck_coin_holds_status")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_coin_holds_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_coin_holds_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coin_holds")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_coin_holds_idempotency_key")),
    )
    op.create_index(
        "ix_coin_holds_held_user_id",
        "coin_holds",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("status = 'held'"),
    )
    op.create_table(
        "coin_transactions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("hold_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
            "'admin_grant', 'admin_correction')",
            name=op.f("ck_coin_transactions_kind"),
        ),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_coin_transactions_nonzero")),
        sa.CheckConstraint(
            "(kind IN ('welcome_bonus', 'referral_reward', 'daily_reward', 'admin_grant')) "
            "= (amount > 0)",
            name=op.f("ck_coin_transactions_sign"),
        ),
        sa.CheckConstraint(
            "hold_id IS NULL OR kind = 'paid_match'", name=op.f("ck_coin_transactions_hold_kind")
        ),
        sa.CheckConstraint(
            "kind NOT IN ('admin_grant', 'admin_correction') OR note IS NOT NULL",
            name=op.f("ck_coin_transactions_admin_note"),
        ),
        sa.ForeignKeyConstraint(
            ["hold_id"], ["coin_holds.id"], name=op.f("fk_coin_transactions_hold_id_coin_holds")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_coin_transactions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coin_transactions")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_coin_transactions_idempotency_key")),
    )
    op.create_index(
        "ix_coin_transactions_user_id_id", "coin_transactions", ["user_id", "id"], unique=False
    )
    op.create_index(
        "uq_coin_transactions_hold_id",
        "coin_transactions",
        ["hold_id"],
        unique=True,
        postgresql_where=sa.text("hold_id IS NOT NULL"),
    )

    # Append-only at the database boundary, like the admin audit log: any UPDATE,
    # DELETE, or TRUNCATE of the ledger fails. Corrections are compensating entries.
    # The table owner could still disable the triggers (O-011).
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER coin_transactions_append_only BEFORE UPDATE OR DELETE "
        "ON coin_transactions FOR EACH ROW EXECUTE FUNCTION coin_transactions_append_only()"
    )
    op.execute(
        "CREATE TRIGGER coin_transactions_no_truncate BEFORE TRUNCATE ON coin_transactions "
        "FOR EACH STATEMENT EXECUTE FUNCTION coin_transactions_append_only()"
    )
    # A hold is never deleted, and changes only once: from held to captured or
    # released, with nothing else altered. A ledger entry for a hold must be the
    # capture of that same hold: captured, the same user, and exactly its amount.
    op.execute(HOLD_TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER coin_holds_transition BEFORE UPDATE OR DELETE ON coin_holds "
        "FOR EACH ROW EXECUTE FUNCTION coin_holds_transition()"
    )
    op.execute(HOLD_ENTRY_FUNCTION)
    op.execute(
        "CREATE TRIGGER coin_transactions_hold_matches BEFORE INSERT ON coin_transactions "
        "FOR EACH ROW WHEN (NEW.hold_id IS NOT NULL) "
        "EXECUTE FUNCTION coin_transactions_hold_matches()"
    )
    op.drop_constraint(op.f("ck_admin_audit_log_action"), "admin_audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_audit_log_action"),
        "admin_audit_log",
        "action IN ('role_changed', 'setting_changed', 'coins_adjusted')",
    )


def downgrade() -> None:
    # The audit log is append-only, so entries for coin adjustments cannot be removed;
    # downgrading is refused while any exist rather than deleting audit history.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM admin_audit_log WHERE action = 'coins_adjusted') "
        "THEN RAISE EXCEPTION 'admin_audit_log has coin adjustments; cannot downgrade'; "
        "END IF; END $$"
    )
    op.drop_constraint(op.f("ck_admin_audit_log_action"), "admin_audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_audit_log_action"),
        "admin_audit_log",
        "action IN ('role_changed', 'setting_changed')",
    )
    op.execute("DROP TRIGGER coin_transactions_hold_matches ON coin_transactions")
    op.execute("DROP FUNCTION coin_transactions_hold_matches()")
    op.execute("DROP TRIGGER coin_holds_transition ON coin_holds")
    op.execute("DROP FUNCTION coin_holds_transition()")
    op.execute("DROP TRIGGER coin_transactions_no_truncate ON coin_transactions")
    op.execute("DROP TRIGGER coin_transactions_append_only ON coin_transactions")
    op.execute("DROP FUNCTION coin_transactions_append_only()")
    op.drop_index(
        "uq_coin_transactions_hold_id",
        table_name="coin_transactions",
        postgresql_where=sa.text("hold_id IS NOT NULL"),
    )
    op.drop_index("ix_coin_transactions_user_id_id", table_name="coin_transactions")
    op.drop_table("coin_transactions")
    op.drop_index(
        "ix_coin_holds_held_user_id",
        table_name="coin_holds",
        postgresql_where=sa.text("status = 'held'"),
    )
    op.drop_table("coin_holds")
