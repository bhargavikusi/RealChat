"""add profile change history

Append-only history of age and gender changes, used for the change cooldown (D-019, D-024).

Revision ID: 5ec24bbfd30d
Revises: 330390156fe2
Create Date: 2026-10-27 17:39:47.321017+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5ec24bbfd30d"
down_revision: str | Sequence[str] | None = "330390156fe2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=False),
        sa.Column("old_value", sa.String(length=16), nullable=False),
        sa.Column("new_value", sa.String(length=16), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("field IN ('age', 'gender')", name=op.f("ck_profile_changes_field")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_profile_changes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_changes")),
    )
    op.create_index(
        "ix_profile_changes_user_field_changed_at",
        "profile_changes",
        ["user_id", "field", "changed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_profile_changes_user_field_changed_at", table_name="profile_changes")
    op.drop_table("profile_changes")
