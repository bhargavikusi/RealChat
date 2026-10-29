"""add idle tracking to conversations

Last activity and idle-warning times on conversations, an index for finding quiet
open chats, and the "idle" end reason (D-039).

Revision ID: dead2291cd8f
Revises: fc4bb28d2eba
Create Date: 2026-10-29 05:36:24.674419+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dead2291cd8f"
down_revision: str | Sequence[str] | None = "fc4bb28d2eba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"),
        "conversations",
        "end_reason IN ('user_ended', 'unreachable', 'idle')",
    )
    op.add_column(
        "conversations",
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations", sa.Column("idle_warned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_conversations_open_last_activity_at",
        "conversations",
        ["last_activity_at"],
        unique=False,
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    # The previous schema has no "idle" reason; idle endings become "user_ended", still
    # with no user recorded as having ended them.
    op.execute("UPDATE conversations SET end_reason = 'user_ended' WHERE end_reason = 'idle'")
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"),
        "conversations",
        "end_reason IN ('user_ended', 'unreachable')",
    )
    op.drop_index(
        "ix_conversations_open_last_activity_at",
        table_name="conversations",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_column("conversations", "idle_warned_at")
    op.drop_column("conversations", "last_activity_at")
