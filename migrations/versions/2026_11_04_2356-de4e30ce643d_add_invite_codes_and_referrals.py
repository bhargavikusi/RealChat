"""add invite codes and referrals

Personal invite codes and who invited whom, for the welcome bonus and invite rewards
(D-050).

Revision ID: de4e30ce643d
Revises: 1fddac321564
Create Date: 2026-11-04 23:56:03.652674+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "de4e30ce643d"
down_revision: str | Sequence[str] | None = "1fddac321564"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REFERRAL_TRANSITION_FUNCTION = """
CREATE FUNCTION referrals_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'referrals cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.status <> 'pending'
        OR NEW.status NOT IN ('rewarded', 'not_rewarded')
        OR (NEW.invitee_user_id, NEW.inviter_user_id, NEW.created_at)
            IS DISTINCT FROM (OLD.invitee_user_id, OLD.inviter_user_id, OLD.created_at)
    THEN
        RAISE EXCEPTION 'a referral can only go from pending to rewarded or not_rewarded'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_invite_codes_user_id_users")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_invite_codes")),
        sa.UniqueConstraint("code", name=op.f("uq_invite_codes_code")),
    )
    op.create_table(
        "referrals",
        sa.Column("invitee_user_id", sa.BigInteger(), nullable=False),
        sa.Column("inviter_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending') = (settled_at IS NULL)", name=op.f("ck_referrals_settled_pair")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'rewarded', 'not_rewarded')", name=op.f("ck_referrals_status")
        ),
        sa.CheckConstraint(
            "invitee_user_id <> inviter_user_id", name=op.f("ck_referrals_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["invitee_user_id"], ["users.id"], name=op.f("fk_referrals_invitee_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["inviter_user_id"], ["users.id"], name=op.f("fk_referrals_inviter_user_id_users")
        ),
        sa.PrimaryKeyConstraint("invitee_user_id", name=op.f("pk_referrals")),
    )
    op.create_index(
        "ix_referrals_inviter_settled_at",
        "referrals",
        ["inviter_user_id", "settled_at"],
        unique=False,
    )

    # A referral is settled once (pending to rewarded or not_rewarded) and never
    # deleted, so the daily cap counts from a history that cannot be rewritten.
    op.execute(REFERRAL_TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER referrals_transition BEFORE UPDATE OR DELETE ON referrals "
        "FOR EACH ROW EXECUTE FUNCTION referrals_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER referrals_transition ON referrals")
    op.execute("DROP FUNCTION referrals_transition()")
    op.drop_index("ix_referrals_inviter_settled_at", table_name="referrals")
    op.drop_table("referrals")
    op.drop_table("invite_codes")
