"""add view-once media

View-once fields on chat messages: the flag, the media length, and when and as which
message it was opened (D-048).

Revision ID: 2ee64b2df2f8
Revises: 5a6122fe7821
Create Date: 2026-10-30 22:52:34.588885+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2ee64b2df2f8"
down_revision: str | Sequence[str] | None = "5a6122fe7821"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("view_once", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("chat_messages", sa.Column("media_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "chat_messages", sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("chat_messages", sa.Column("viewed_message_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_chat_messages_viewed_once"), "chat_messages", "viewed_at IS NULL OR view_once"
    )
    op.create_check_constraint(
        op.f("ck_chat_messages_viewed_message_pair"),
        "chat_messages",
        "viewed_message_id IS NULL OR viewed_at IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_chat_messages_viewed_message_pair"), "chat_messages", type_="check")
    op.drop_constraint(op.f("ck_chat_messages_viewed_once"), "chat_messages", type_="check")
    op.drop_column("chat_messages", "viewed_message_id")
    op.drop_column("chat_messages", "viewed_at")
    op.drop_column("chat_messages", "media_seconds")
    op.drop_column("chat_messages", "view_once")
