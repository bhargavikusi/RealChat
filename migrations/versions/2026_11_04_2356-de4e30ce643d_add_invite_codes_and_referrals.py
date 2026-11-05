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


def downgrade() -> None:
    op.drop_index("ix_referrals_inviter_settled_at", table_name="referrals")
    op.drop_table("referrals")
    op.drop_table("invite_codes")
