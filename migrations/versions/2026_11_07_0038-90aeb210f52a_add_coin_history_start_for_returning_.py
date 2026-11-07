"""add coin history start for returning users (D-055)

Revision ID: 90aeb210f52a
Revises: 2a724f8b7a48
Create Date: 2026-11-07 00:38:12.734672+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "90aeb210f52a"
down_revision: str | Sequence[str] | None = "2a724f8b7a48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("coin_history_after_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "coin_history_after_id")
