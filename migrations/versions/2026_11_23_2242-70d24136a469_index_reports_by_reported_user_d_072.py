"""index reports by reported user (D-072)

Revision ID: 70d24136a469
Revises: f3a34bf5e5b5
Create Date: 2026-11-23 22:42:37.728574+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "70d24136a469"
down_revision: str | Sequence[str] | None = "f3a34bf5e5b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_reports_reported_user_id", "reports", ["reported_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_reports_reported_user_id", table_name="reports")
