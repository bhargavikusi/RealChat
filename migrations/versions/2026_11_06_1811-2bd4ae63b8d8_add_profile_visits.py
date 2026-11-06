"""add profile visits (D-053)

Revision ID: 2bd4ae63b8d8
Revises: bdfaf8d14f37
Create Date: 2026-11-06 18:11:01.253971+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2bd4ae63b8d8"
down_revision: str | Sequence[str] | None = "bdfaf8d14f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_visits",
        sa.Column("visitor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("visited_user_id", sa.BigInteger(), nullable=False),
        sa.Column("last_visited_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "visitor_user_id <> visited_user_id", name=op.f("ck_profile_visits_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["visited_user_id"],
            ["users.id"],
            name=op.f("fk_profile_visits_visited_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["visitor_user_id"],
            ["users.id"],
            name=op.f("fk_profile_visits_visitor_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "visitor_user_id", "visited_user_id", name=op.f("pk_profile_visits")
        ),
    )
    op.create_index(
        "ix_profile_visits_last_visited_at", "profile_visits", ["last_visited_at"], unique=False
    )
    op.create_index(
        "ix_profile_visits_visited_latest",
        "profile_visits",
        ["visited_user_id", "last_visited_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_profile_visits_visited_latest", table_name="profile_visits")
    op.drop_index("ix_profile_visits_last_visited_at", table_name="profile_visits")
    op.drop_table("profile_visits")
