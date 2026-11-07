"""add profile likes (D-056)

Revision ID: 95babc71da96
Revises: 90aeb210f52a
Create Date: 2026-11-07 04:59:42.397532+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "95babc71da96"
down_revision: str | Sequence[str] | None = "90aeb210f52a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_likes",
        sa.Column("liker_user_id", sa.BigInteger(), nullable=False),
        sa.Column("liked_user_id", sa.BigInteger(), nullable=False),
        sa.Column("liked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "liker_user_id <> liked_user_id", name=op.f("ck_profile_likes_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["liked_user_id"],
            ["users.id"],
            name=op.f("fk_profile_likes_liked_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["liker_user_id"],
            ["users.id"],
            name=op.f("fk_profile_likes_liker_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("liker_user_id", "liked_user_id", name=op.f("pk_profile_likes")),
    )
    op.create_index(
        "ix_profile_likes_liked_latest",
        "profile_likes",
        ["liked_user_id", "liked_at"],
        unique=False,
    )
    op.create_index(
        "ix_profile_likes_liker_latest",
        "profile_likes",
        ["liker_user_id", "liked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_profile_likes_liker_latest", table_name="profile_likes")
    op.drop_index("ix_profile_likes_liked_latest", table_name="profile_likes")
    op.drop_table("profile_likes")
