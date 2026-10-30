"""add private mode

Private mode sessions, the link from chat messages to their session, and the queue of
Telegram messages to delete (D-031, D-047).

Revision ID: 5a6122fe7821
Revises: b7a10ab0be84
Create Date: 2026-10-30 20:37:20.836187+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a6122fe7821"
down_revision: str | Sequence[str] | None = "b7a10ab0be84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_deletions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("not_before < expires_at", name=op.f("ck_telegram_deletions_window")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_deletions")),
        sa.UniqueConstraint(
            "chat_id", "message_id", name=op.f("uq_telegram_deletions_chat_id_message_id")
        ),
    )
    op.create_index(
        "ix_telegram_deletions_not_before", "telegram_deletions", ["not_before"], unique=False
    )
    op.create_table(
        "private_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("started_by_user_id", sa.BigInteger(), nullable=False),
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
            "end_reason IN ('turned_off', 'chat_ended', 'time_limit')",
            name=op.f("ck_private_sessions_end_reason"),
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (end_reason IS NULL)", name=op.f("ck_private_sessions_ended_pair")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_private_sessions_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["users.id"],
            name=op.f("fk_private_sessions_ended_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["started_by_user_id"],
            ["users.id"],
            name=op.f("fk_private_sessions_started_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_private_sessions")),
    )
    op.create_index(
        "ix_private_sessions_active_started_at",
        "private_sessions",
        ["started_at"],
        unique=False,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "uq_private_sessions_active_conversation",
        "private_sessions",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.add_column("chat_messages", sa.Column("private_session_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_chat_messages_private_session_id_private_sessions"),
        "chat_messages",
        "private_sessions",
        ["private_session_id"],
        ["id"],
    )
    op.create_index(
        "ix_chat_messages_private_session_id",
        "chat_messages",
        ["private_session_id"],
        unique=False,
        postgresql_where=sa.text("private_session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_messages_private_session_id",
        table_name="chat_messages",
        postgresql_where=sa.text("private_session_id IS NOT NULL"),
    )
    op.drop_constraint(
        op.f("fk_chat_messages_private_session_id_private_sessions"),
        "chat_messages",
        type_="foreignkey",
    )
    op.drop_column("chat_messages", "private_session_id")
    op.drop_index(
        "uq_private_sessions_active_conversation",
        table_name="private_sessions",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_index(
        "ix_private_sessions_active_started_at",
        table_name="private_sessions",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_table("private_sessions")
    op.drop_index("ix_telegram_deletions_not_before", table_name="telegram_deletions")
    op.drop_table("telegram_deletions")
