"""index chat messages by creation time

Serves the daily deletion of expired chat messages, oldest first (D-046).

Revision ID: b7a10ab0be84
Revises: 481eb738689a
Create Date: 2026-10-30 05:21:49.682574+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7a10ab0be84"
down_revision: str | Sequence[str] | None = "481eb738689a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chat_messages_created_at", table_name="chat_messages")
