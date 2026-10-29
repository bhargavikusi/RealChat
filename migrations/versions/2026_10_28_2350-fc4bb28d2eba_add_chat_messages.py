"""add chat messages

Relayed chat messages kept for moderation (D-028), and the "unreachable" end reason
for conversations whose partner can no longer receive messages.

Revision ID: fc4bb28d2eba
Revises: 82b0b6af5165
Create Date: 2026-10-28 23:50:19.579637+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fc4bb28d2eba"
down_revision: str | Sequence[str] | None = "82b0b6af5165"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"),
        "conversations",
        "end_reason IN ('user_ended', 'unreachable')",
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_user_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_message_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_message_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("edited_text", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('text', 'photo', 'video', 'animation', 'audio', 'voice', 'video_note', "
            "'sticker', 'document', 'poll', 'dice', 'other')",
            name=op.f("ck_chat_messages_kind"),
        ),
        sa.CheckConstraint(
            "(edited_text IS NULL) OR (edited_at IS NOT NULL)",
            name=op.f("ck_chat_messages_edited_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_chat_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"], ["users.id"], name=op.f("fk_chat_messages_sender_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
        sa.UniqueConstraint(
            "sender_user_id",
            "sender_message_id",
            name=op.f("uq_chat_messages_sender_user_id_sender_message_id"),
        ),
    )
    op.create_index(
        "ix_chat_messages_conversation_recipient",
        "chat_messages",
        ["conversation_id", "recipient_message_id"],
        unique=False,
    )


def downgrade() -> None:
    # Conversations that ended as unreachable are recorded as ended by the user, the only
    # reason the previous schema knows.
    op.execute(
        "UPDATE conversations SET end_reason = 'user_ended' WHERE end_reason = 'unreachable'"
    )
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"), "conversations", "end_reason IN ('user_ended')"
    )
    op.drop_index("ix_chat_messages_conversation_recipient", table_name="chat_messages")
    op.drop_table("chat_messages")
