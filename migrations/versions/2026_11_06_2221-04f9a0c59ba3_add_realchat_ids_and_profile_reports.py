"""add RealChat IDs, and blocks and reports without a chat (D-054)

Revision ID: 04f9a0c59ba3
Revises: 2bd4ae63b8d8
Create Date: 2026-11-06 22:21:04.077675+00:00

"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "04f9a0c59ba3"
down_revision: str | Sequence[str] | None = "2bd4ae63b8d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# As in backend.modules.users.public_ids, copied so this migration never changes.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
LENGTH = 7
BATCH_SIZE = 1_000


def upgrade() -> None:
    op.add_column("profiles", sa.Column("public_id", sa.String(length=LENGTH), nullable=True))
    # Every existing profile gets its own random ID before the column becomes required,
    # a batch of profiles at a time, each batch written with one executemany.
    connection = op.get_bind()
    taken: set[str] = set()
    while True:
        user_ids = (
            connection.execute(
                sa.text("SELECT user_id FROM profiles WHERE public_id IS NULL LIMIT :limit"),
                {"limit": BATCH_SIZE},
            )
            .scalars()
            .all()
        )
        if not user_ids:
            break
        rows = []
        for user_id in user_ids:
            public_id = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
            while public_id in taken:
                public_id = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
            taken.add(public_id)
            rows.append({"public_id": public_id, "user_id": user_id})
        connection.execute(
            sa.text("UPDATE profiles SET public_id = :public_id WHERE user_id = :user_id"), rows
        )
    op.alter_column("profiles", "public_id", nullable=False)
    op.create_unique_constraint(op.f("uq_profiles_public_id"), "profiles", ["public_id"])
    op.create_check_constraint(
        op.f("ck_profiles_public_id_format"),
        "profiles",
        f"public_id ~ '^[{ALPHABET}]{{{LENGTH}}}$'",
    )
    op.alter_column("reports", "conversation_id", existing_type=sa.BIGINT(), nullable=True)
    op.create_index(
        "uq_reports_profile_pair",
        "reports",
        ["reporter_user_id", "reported_user_id"],
        unique=True,
        postgresql_where=sa.text("conversation_id IS NULL"),
    )
    op.alter_column("user_blocks", "conversation_id", existing_type=sa.BIGINT(), nullable=True)


def downgrade() -> None:
    # Blocks and reports without a chat cannot exist before this revision, so a downgrade
    # deletes them.
    op.execute("DELETE FROM user_blocks WHERE conversation_id IS NULL")
    op.alter_column("user_blocks", "conversation_id", existing_type=sa.BIGINT(), nullable=False)
    op.drop_index(
        "uq_reports_profile_pair",
        table_name="reports",
        postgresql_where=sa.text("conversation_id IS NULL"),
    )
    op.execute("DELETE FROM reports WHERE conversation_id IS NULL")
    op.alter_column("reports", "conversation_id", existing_type=sa.BIGINT(), nullable=False)
    op.drop_constraint(op.f("ck_profiles_public_id_format"), "profiles", type_="check")
    op.drop_constraint(op.f("uq_profiles_public_id"), "profiles", type_="unique")
    op.drop_column("profiles", "public_id")
