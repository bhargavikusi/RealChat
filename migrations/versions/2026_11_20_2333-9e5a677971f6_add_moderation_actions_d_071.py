"""add moderation actions (D-071)

Revision ID: 9e5a677971f6
Revises: c40b9ea815e5
Create Date: 2026-11-20 23:33:24.152063+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e5a677971f6"
down_revision: str | Sequence[str] | None = "c40b9ea815e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RULES = (
    "'spam', 'harassment', 'sexual', 'under_18', 'threats', 'impersonation', "
    "'private_info', 'evasion', 'other'"
)
OLD_ACTIONS = (
    "'role_changed', 'setting_changed', 'coins_adjusted', 'purchase_refunded', 'payment_recorded'"
)
NEW_ACTIONS = OLD_ACTIONS + (
    ", 'report_closed', 'account_warned', 'account_suspended', 'account_banned', "
    "'restriction_lifted'"
)
OLD_END_REASONS = "'user_ended', 'unreachable', 'idle', 'blocked', 'reported'"
NEW_END_REASONS = OLD_END_REASONS + ", 'restricted'"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.create_table(
        "moderation_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("rule", sa.String(length=16), nullable=True),
        sa.Column("days", sa.SmallInteger(), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'suspend') = (days IS NOT NULL)", name=op.f("ck_moderation_actions_days_pair")
        ),
        sa.CheckConstraint(
            "(kind IN ('warn', 'suspend', 'ban')) = (rule IS NOT NULL)",
            name=op.f("ck_moderation_actions_rule_pair"),
        ),
        sa.CheckConstraint(
            "kind <> 'close' OR report_id IS NOT NULL",
            name=op.f("ck_moderation_actions_close_needs_report"),
        ),
        sa.CheckConstraint(
            "kind IN ('close', 'warn', 'suspend', 'ban', 'lift')",
            name=op.f("ck_moderation_actions_kind"),
        ),
        sa.CheckConstraint(
            f"rule IN ({RULES})",
            name=op.f("ck_moderation_actions_rule"),
        ),
        sa.CheckConstraint(
            "(days IS NULL) = (ends_at IS NULL)", name=op.f("ck_moderation_actions_ends_pair")
        ),
        sa.CheckConstraint(
            "actor_user_id <> target_user_id", name=op.f("ck_moderation_actions_not_self")
        ),
        sa.CheckConstraint("days IN (1, 3, 7, 30)", name=op.f("ck_moderation_actions_days")),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=op.f("fk_moderation_actions_actor_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["reports.id"], name=op.f("fk_moderation_actions_report_id_reports")
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["users.id"],
            name=op.f("fk_moderation_actions_target_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_actions")),
    )
    op.create_index(
        "ix_moderation_actions_target_created_at",
        "moderation_actions",
        ["target_user_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "moderation_notices",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("action_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("recipient_user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = 'chat_ended') = (conversation_id IS NOT NULL)",
            name=op.f("ck_moderation_notices_conversation_pair"),
        ),
        sa.CheckConstraint(
            "kind IN ('action', 'reporter', 'chat_ended')", name=op.f("ck_moderation_notices_kind")
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["moderation_actions.id"],
            name=op.f("fk_moderation_notices_action_id_moderation_actions"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_moderation_notices_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            name=op.f("fk_moderation_notices_recipient_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_notices")),
        sa.UniqueConstraint(
            "action_id",
            "kind",
            "recipient_user_id",
            name=op.f("uq_moderation_notices_action_id_kind_recipient_user_id"),
        ),
    )
    op.create_index(
        "ix_moderation_notices_due",
        "moderation_notices",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.add_column("reports", sa.Column("outcome", sa.String(length=16), nullable=True))
    op.add_column("reports", sa.Column("closed_by_action_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_reports_closed_by_action_id_moderation_actions"),
        "reports",
        "moderation_actions",
        ["closed_by_action_id"],
        ["id"],
        use_alter=True,
    )
    # Reports closed before moderation tools (by hand) count as closed with no action.
    op.execute("UPDATE reports SET outcome = 'no_action' WHERE status = 'closed'")
    op.create_check_constraint(
        op.f("ck_reports_outcome"), "reports", "outcome IN ('no_action', 'action_taken')"
    )
    op.create_check_constraint(
        op.f("ck_reports_outcome_pair"), "reports", "(status = 'closed') = (outcome IS NOT NULL)"
    )
    op.add_column("users", sa.Column("suspended_until", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_suspended_until_status"),
        "users",
        "suspended_until IS NULL OR status = 'suspended'",
    )
    op.create_index(
        "ix_users_suspended_until",
        "users",
        ["suspended_until"],
        unique=False,
        postgresql_where=sa.text("status = 'suspended'"),
    )
    _replace_check(
        "conversations", "ck_conversations_end_reason", f"end_reason IN ({NEW_END_REASONS})"
    )
    _replace_check("admin_audit_log", "ck_admin_audit_log_action", f"action IN ({NEW_ACTIONS})")


def downgrade() -> None:
    # The audit log is append-only: this fails while any moderation entry exists, rather
    # than deleting audit history. Likewise for chats a restriction ended.
    _replace_check("admin_audit_log", "ck_admin_audit_log_action", f"action IN ({OLD_ACTIONS})")
    _replace_check(
        "conversations", "ck_conversations_end_reason", f"end_reason IN ({OLD_END_REASONS})"
    )
    op.drop_index(
        "ix_users_suspended_until",
        table_name="users",
        postgresql_where=sa.text("status = 'suspended'"),
    )
    op.drop_constraint(op.f("ck_users_suspended_until_status"), "users", type_="check")
    op.drop_column("users", "suspended_until")
    op.drop_constraint(op.f("ck_reports_outcome_pair"), "reports", type_="check")
    op.drop_constraint(op.f("ck_reports_outcome"), "reports", type_="check")
    op.drop_constraint(
        op.f("fk_reports_closed_by_action_id_moderation_actions"), "reports", type_="foreignkey"
    )
    op.drop_column("reports", "closed_by_action_id")
    op.drop_column("reports", "outcome")
    op.drop_index(
        "ix_moderation_notices_due",
        table_name="moderation_notices",
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.drop_table("moderation_notices")
    op.drop_index("ix_moderation_actions_target_created_at", table_name="moderation_actions")
    op.drop_table("moderation_actions")
