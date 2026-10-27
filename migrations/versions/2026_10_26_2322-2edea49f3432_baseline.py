"""baseline

Empty starting point for the schema. Domain tables are added by later migrations.

Revision ID: 2edea49f3432
Revises:
Create Date: 2026-10-26 23:22:16.814398+00:00

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "2edea49f3432"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
