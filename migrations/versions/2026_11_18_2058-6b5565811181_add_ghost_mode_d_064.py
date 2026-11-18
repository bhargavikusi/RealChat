"""add ghost mode (D-064)

Revision ID: 6b5565811181
Revises: 18c5a1703fb8
Create Date: 2026-11-18 20:58:27.604682+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b5565811181"
down_revision: str | Sequence[str] | None = "18c5a1703fb8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ghost_mode", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "ghost_mode")
