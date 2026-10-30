"""add blocks and reports

The user_blocks and reports tables, and the "blocked" and "reported" end reasons
(D-037, D-045).

Revision ID: 481eb738689a
Revises: dead2291cd8f
Create Date: 2026-10-29 22:32:46.994127+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "481eb738689a"
down_revision: str | Sequence[str] | None = "dead2291cd8f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"),
        "conversations",
        "end_reason IN ('user_ended', 'unreachable', 'idle', 'blocked', 'reported')",
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("reporter_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reported_user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("details", sa.String(length=500), nullable=True),
        sa.Column("urgent", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)", name=op.f("ck_reports_closed_pair")
        ),
        sa.CheckConstraint(
            "reason IN ('spam', 'harassment', 'sexual', 'under_18', 'threats', 'other')",
            name=op.f("ck_reports_reason"),
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_reports_status")),
        sa.CheckConstraint(
            "char_length(details) BETWEEN 1 AND 500", name=op.f("ck_reports_details")
        ),
        sa.CheckConstraint(
            "reporter_user_id <> reported_user_id", name=op.f("ck_reports_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_reports_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["reported_user_id"], ["users.id"], name=op.f("fk_reports_reported_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["reporter_user_id"], ["users.id"], name=op.f("fk_reports_reporter_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
        sa.UniqueConstraint(
            "reporter_user_id",
            "conversation_id",
            name=op.f("uq_reports_reporter_user_id_conversation_id"),
        ),
    )
    op.create_index(
        "ix_reports_open_conversation_id",
        "reports",
        ["conversation_id"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_reports_reporter_created_at",
        "reports",
        ["reporter_user_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "user_blocks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("blocker_user_id", sa.BigInteger(), nullable=False),
        sa.Column("blocked_user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "blocked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("unblocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "blocker_user_id <> blocked_user_id", name=op.f("ck_user_blocks_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["blocked_user_id"], ["users.id"], name=op.f("fk_user_blocks_blocked_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["blocker_user_id"], ["users.id"], name=op.f("fk_user_blocks_blocker_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_user_blocks_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_blocks")),
    )
    op.create_index(
        "uq_user_blocks_active_pair",
        "user_blocks",
        ["blocker_user_id", "blocked_user_id"],
        unique=True,
        postgresql_where=sa.text("unblocked_at IS NULL"),
    )


def downgrade() -> None:
    # The previous schema has no "blocked" or "reported" reason; those endings become
    # "user_ended", still recorded as ended by the user who blocked or reported.
    op.execute(
        "UPDATE conversations SET end_reason = 'user_ended'"
        " WHERE end_reason IN ('blocked', 'reported')"
    )
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"),
        "conversations",
        "end_reason IN ('user_ended', 'unreachable', 'idle')",
    )
    op.drop_index(
        "uq_user_blocks_active_pair",
        table_name="user_blocks",
        postgresql_where=sa.text("unblocked_at IS NULL"),
    )
    op.drop_table("user_blocks")
    op.drop_index("ix_reports_reporter_created_at", table_name="reports")
    op.drop_index(
        "ix_reports_open_conversation_id",
        table_name="reports",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_table("reports")
