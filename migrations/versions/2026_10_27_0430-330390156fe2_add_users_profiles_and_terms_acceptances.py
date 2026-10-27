"""add users profiles and terms acceptances

Users keyed by Telegram user ID, one profile per user, and versioned terms acceptances.

Revision ID: 330390156fe2
Revises: 2edea49f3432
Create Date: 2026-10-27 04:30:20.937112+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "330390156fe2"
down_revision: str | Sequence[str] | None = "2edea49f3432"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'banned', 'deleted')", name=op.f("ck_users_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_users_telegram_user_id")),
    )
    op.create_table(
        "profiles",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("nickname", sa.String(length=20), nullable=True),
        sa.Column("age_at_entry", sa.SmallInteger(), nullable=True),
        sa.Column("age_entered_on", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(length=8), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("city", sa.String(length=60), nullable=True),
        sa.Column("bio", sa.String(length=300), nullable=True),
        sa.Column("photo_file_id", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(length=8), server_default="visible", nullable=False),
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "country_code ~ '^[A-Z]{2}$'", name=op.f("ck_profiles_country_code_format")
        ),
        sa.CheckConstraint(
            "gender IN ('male', 'female', 'other')", name=op.f("ck_profiles_gender")
        ),
        sa.CheckConstraint(
            "visibility IN ('visible', 'hidden')", name=op.f("ck_profiles_visibility")
        ),
        sa.CheckConstraint(
            "(age_at_entry IS NULL) = (age_entered_on IS NULL)", name=op.f("ck_profiles_age_pair")
        ),
        sa.CheckConstraint("age_at_entry BETWEEN 13 AND 99", name=op.f("ck_profiles_age_range")),
        sa.CheckConstraint(
            "char_length(bio) BETWEEN 1 AND 300", name=op.f("ck_profiles_bio_length")
        ),
        sa.CheckConstraint(
            "char_length(city) BETWEEN 1 AND 60", name=op.f("ck_profiles_city_length")
        ),
        sa.CheckConstraint(
            "char_length(nickname) BETWEEN 2 AND 20", name=op.f("ck_profiles_nickname_length")
        ),
        sa.CheckConstraint(
            "onboarding_completed_at IS NULL OR (nickname IS NOT NULL"
            " AND age_at_entry IS NOT NULL AND gender IS NOT NULL AND country_code IS NOT NULL)",
            name=op.f("ck_profiles_complete_requires_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_profiles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_profiles")),
    )
    op.create_table(
        "terms_acceptances",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("terms_version", sa.String(length=32), nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_terms_acceptances_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_terms_acceptances")),
        sa.UniqueConstraint(
            "user_id", "terms_version", name=op.f("uq_terms_acceptances_user_id_terms_version")
        ),
    )


def downgrade() -> None:
    op.drop_table("terms_acceptances")
    op.drop_table("profiles")
    op.drop_table("users")
