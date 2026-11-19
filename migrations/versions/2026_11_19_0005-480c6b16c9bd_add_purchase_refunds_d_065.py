"""add purchase refunds (D-065)

Revision ID: 480c6b16c9bd
Revises: 6b5565811181
Create Date: 2026-11-19 00:05:51.861398+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "480c6b16c9bd"
down_revision: str | Sequence[str] | None = "6b5565811181"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_KINDS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
    "'admin_grant', 'admin_correction', 'account_deleted', 'direct_message', "
    "'chat_request', 'activity_alert', 'activity_alert_refund', 'coin_purchase', "
    "'vip_coins'"
)
NEW_KINDS = OLD_KINDS + ", 'purchase_refund'"
# Which kinds add coins is unchanged: a refund removes them.
CREDITS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'admin_grant', "
    "'activity_alert_refund', 'coin_purchase', 'vip_coins'"
)
OLD_ACTIONS = "'role_changed', 'setting_changed', 'coins_adjusted'"
NEW_ACTIONS = OLD_ACTIONS + ", 'purchase_refunded'"
OLD_REASONS = "'account_deleted'"
NEW_REASONS = OLD_REASONS + ", 'admin', 'telegram'"

# The payment guard from 18c5a1703fb8. {refund_fields} adds, from this revision, that
# a refund's note and shortfall are set when it leaves `paid` and never change after.
PAYMENT_TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION stars_payments_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'stars payments cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.telegram_charge_id, NEW.user_id, NEW.invoice_id, NEW.stars, NEW.created_at,
            NEW.vip_until)
            IS DISTINCT FROM
            (OLD.telegram_charge_id, OLD.user_id, OLD.invoice_id, OLD.stars, OLD.created_at,
            OLD.vip_until)
        {refund_fields}
        OR NOT (
            (OLD.status = 'paid' AND NEW.status IN ('refund_due', 'refunded'))
            OR (OLD.status = 'refund_due' AND NEW.status IN ('refund_due', 'refunded')
                AND NEW.refund_reason IS NOT DISTINCT FROM OLD.refund_reason)
        )
    THEN
        RAISE EXCEPTION 'a stars payment can only move from paid to refund_due to refunded'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""
REFUND_FIELDS = """OR (OLD.status <> 'paid'
            AND (NEW.refund_note, NEW.refund_shortfall)
                IS DISTINCT FROM (OLD.refund_note, OLD.refund_shortfall))"""


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def _checks(kinds: str, actions: str, reasons: str) -> None:
    _replace_check("coin_transactions", "ck_coin_transactions_kind", f"kind IN ({kinds})")
    _replace_check(
        "coin_transactions", "ck_coin_transactions_sign", f"(kind IN ({CREDITS})) = (amount > 0)"
    )
    _replace_check("admin_audit_log", "ck_admin_audit_log_action", f"action IN ({actions})")
    _replace_check(
        "stars_payments",
        "ck_stars_payments_refund_reason",
        f"refund_reason IS NULL OR refund_reason IN ({reasons})",
    )


def upgrade() -> None:
    _checks(NEW_KINDS, NEW_ACTIONS, NEW_REASONS)
    op.add_column("stars_payments", sa.Column("refund_note", sa.Text(), nullable=True))
    op.add_column("stars_payments", sa.Column("refund_shortfall", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_stars_payments_refund_note"),
        "stars_payments",
        "(refund_reason IS NOT DISTINCT FROM 'admin') = (refund_note IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_stars_payments_shortfall"),
        "stars_payments",
        "refund_shortfall IS NULL OR refund_shortfall > 0",
    )
    op.execute(PAYMENT_TRANSITION_FUNCTION.format(refund_fields=REFUND_FIELDS))


def downgrade() -> None:
    # Like earlier ledger downgrades, the restored checks fail while any refund entry,
    # audit entry, or payment with a new refund reason exists, rather than rewriting
    # history.
    op.execute(PAYMENT_TRANSITION_FUNCTION.format(refund_fields=""))
    op.drop_constraint(op.f("ck_stars_payments_shortfall"), "stars_payments", type_="check")
    op.drop_constraint(op.f("ck_stars_payments_refund_note"), "stars_payments", type_="check")
    _checks(OLD_KINDS, OLD_ACTIONS, OLD_REASONS)
    op.drop_column("stars_payments", "refund_shortfall")
    op.drop_column("stars_payments", "refund_note")
