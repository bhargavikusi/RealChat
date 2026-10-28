"""add conversations

Conversations between matched users and their participants. A partial unique index
allows each user at most one open conversation (D-027).

Revision ID: 82b0b6af5165
Revises: 5f154097ffb6
Create Date: 2026-10-28 19:45:26.952980+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "82b0b6af5165"
down_revision: str | Sequence[str] | None = "5f154097ffb6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=16), nullable=True),
        sa.Column("ended_by_user_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "end_reason IN ('user_ended')", name=op.f("ck_conversations_end_reason")
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (end_reason IS NULL)", name=op.f("ck_conversations_ended_pair")
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"], ["users.id"], name=op.f("fk_conversations_ended_by_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_table(
        "conversation_participants",
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_participants_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_conversation_participants_user_id_users")
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id", "user_id", name=op.f("pk_conversation_participants")
        ),
    )
    op.create_index(
        "ix_conversation_participants_user_id",
        "conversation_participants",
        ["user_id", "conversation_id"],
        unique=False,
    )
    op.create_index(
        "uq_conversation_participants_open_user",
        "conversation_participants",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_conversation_participants_open_user",
        table_name="conversation_participants",
        postgresql_where=sa.text("left_at IS NULL"),
    )
    op.drop_index("ix_conversation_participants_user_id", table_name="conversation_participants")
    op.drop_table("conversation_participants")
    op.drop_table("conversations")
