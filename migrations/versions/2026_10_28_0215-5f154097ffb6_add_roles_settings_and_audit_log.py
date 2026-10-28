"""add roles, runtime settings, and the admin audit log

Access roles on users, admin-changeable runtime settings, and an append-only audit
log of role and setting changes (D-034, D-035).

Revision ID: 5f154097ffb6
Revises: 5ec24bbfd30d
Create Date: 2026-10-28 02:15:46.585139+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f154097ffb6"
down_revision: str | Sequence[str] | None = "5ec24bbfd30d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_settings")),
    )
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
        sa.Column("setting_key", sa.String(length=64), nullable=True),
        sa.Column("old_value", sa.String(length=64), nullable=True),
        sa.Column("new_value", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('role_changed', 'setting_changed')", name=op.f("ck_admin_audit_log_action")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=op.f("fk_admin_audit_log_actor_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"], name=op.f("fk_admin_audit_log_target_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_audit_log")),
    )
    op.add_column(
        "users", sa.Column("role", sa.String(length=16), server_default="user", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_users_role"), "users", "role IN ('user', 'moderator', 'admin')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_role"), "users", type_="check")
    op.drop_column("users", "role")
    op.drop_table("admin_audit_log")
    op.drop_table("app_settings")
