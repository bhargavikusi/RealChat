"""add VIP pass (D-062)

Revision ID: 18c5a1703fb8
Revises: 4c8f87d0b07f
Create Date: 2026-11-17 05:10:35.890168+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "18c5a1703fb8"
down_revision: str | Sequence[str] | None = "4c8f87d0b07f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'paid_match', "
    "'admin_grant', 'admin_correction', 'account_deleted', 'direct_message', "
    "'chat_request', 'activity_alert', 'activity_alert_refund', 'coin_purchase'"
)
NEW_KINDS = OLD_KINDS + ", 'vip_coins'"
OLD_CREDITS = (
    "'welcome_bonus', 'referral_reward', 'daily_reward', 'admin_grant', "
    "'activity_alert_refund', 'coin_purchase'"
)
NEW_CREDITS = OLD_CREDITS + ", 'vip_coins'"

# The payment guard from 4c8f87d0b07f, with `frozen` the columns that never change.
PAYMENT_TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION stars_payments_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'stars payments cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({new}) IS DISTINCT FROM ({old})
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
OLD_FROZEN = ("telegram_charge_id", "user_id", "invoice_id", "stars", "created_at")
# The VIP end date a payment produced is a record of what was granted.
NEW_FROZEN = (*OLD_FROZEN, "vip_until")


def _payment_guard(frozen: tuple[str, ...]) -> None:
    op.execute(
        PAYMENT_TRANSITION_FUNCTION.format(
            new=", ".join(f"NEW.{c}" for c in frozen), old=", ".join(f"OLD.{c}" for c in frozen)
        )
    )


def _ledger_checks(kinds: str, credits: str) -> None:
    """Replace the ledger checks that list kinds, and which kinds add coins."""
    for name, condition in (
        ("ck_coin_transactions_kind", f"kind IN ({kinds})"),
        ("ck_coin_transactions_sign", f"(kind IN ({credits})) = (amount > 0)"),
    ):
        op.drop_constraint(op.f(name), "coin_transactions", type_="check")
        op.create_check_constraint(op.f(name), "coin_transactions", condition)


def _invoice_checks(products: str, coins: str) -> None:
    for name, condition in (
        ("ck_stars_invoices_product", f"product IN ({products})"),
        ("ck_stars_invoices_coins", coins),
    ):
        op.drop_constraint(op.f(name), "stars_invoices", type_="check")
        op.create_check_constraint(op.f(name), "stars_invoices", condition)


def upgrade() -> None:
    _ledger_checks(NEW_KINDS, NEW_CREDITS)
    op.add_column("stars_invoices", sa.Column("days", sa.Integer(), nullable=True))
    _invoice_checks("'coins', 'vip'", "coins > 0 OR (product = 'vip' AND coins = 0)")
    op.create_check_constraint(
        op.f("ck_stars_invoices_vip_days"),
        "stars_invoices",
        "(product = 'vip') = (days IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_stars_invoices_days"), "stars_invoices", "days IS NULL OR days > 0"
    )
    op.add_column(
        "stars_payments", sa.Column("vip_until", sa.DateTime(timezone=True), nullable=True)
    )
    _payment_guard(NEW_FROZEN)
    op.add_column("users", sa.Column("vip_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Like the ledger checks below, this fails while any VIP invoice or ledger entry
    # exists, rather than rewriting history.
    op.drop_column("users", "vip_until")
    _payment_guard(OLD_FROZEN)
    op.drop_column("stars_payments", "vip_until")
    op.drop_constraint(op.f("ck_stars_invoices_days"), "stars_invoices", type_="check")
    op.drop_constraint(op.f("ck_stars_invoices_vip_days"), "stars_invoices", type_="check")
    _invoice_checks("'coins'", "coins > 0")
    op.drop_column("stars_invoices", "days")
    _ledger_checks(OLD_KINDS, OLD_CREDITS)
