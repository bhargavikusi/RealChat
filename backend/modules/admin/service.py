"""Role, runtime-setting, and coin changes, and purchase refunds, each recorded in the
admin audit log (D-034, D-035, D-050, D-065).

Like the users service, these functions take an AsyncSession and leave committing
to the caller. Roles are changed only by the server-side CLI (`apps/admin_cli.py`);
no bot handler calls `set_role`. The one other role change is account deletion,
which can only lower a role to `user` (D-034).
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.admin.models import AdminAuditEntry, AppSetting
from backend.modules.admin.settings import SPECS, SettingKey, load_values
from backend.modules.coins import service as coins
from backend.modules.payments import service as payments
from backend.modules.payments.models import StarsPayment
from backend.modules.payments.reconcile import TelegramTransaction
from backend.modules.payments.service import (
    CURRENCY,
    Fulfilled,
    Outcome,
    Purchase,
    RefundRefusal,
    RefundRefusedError,
    Reversed,
    UnknownPaymentError,
)
from backend.modules.users.models import ROLES, User


class AdminError(Exception):
    """A rejected administrative change. `message` is safe to show to the admin."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotAdminError(AdminError):
    """The acting account is no longer an active admin."""

    def __init__(self) -> None:
        super().__init__("Admin settings are not available.")


# --- Roles ---------------------------------------------------------------------------


async def get_role(session: AsyncSession, telegram_user_id: int) -> str | None:
    """The role of a Telegram user's account, or None if they have no account."""
    return await session.scalar(select(User.role).where(User.telegram_user_id == telegram_user_id))


async def set_role(session: AsyncSession, telegram_user_id: int, role: str) -> str:
    """Give an existing account a role; returns the previous role.

    The account row is locked, so concurrent changes are applied one after the other
    and each audit entry has the correct old value. An unchanged role is not logged.
    """
    if role not in ROLES:
        raise AdminError(f"Unknown role {role!r}. Choose one of: {', '.join(ROLES)}.")
    user = (
        await session.execute(
            select(User)
            .where(User.telegram_user_id == telegram_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise AdminError("No account for that Telegram user ID. They must start the bot first.")
    old = user.role
    if old != role:
        user.role = role
        session.add(
            AdminAuditEntry(
                actor_user_id=None,
                action="role_changed",
                target_user_id=user.id,
                old_value=old,
                new_value=role,
            )
        )
    return old


# Coin amounts are stored as 32-bit integers.
MAX_COIN_ADJUSTMENT = 2_147_483_647
# Ledger keys are at most 128 characters, and admin keys get an "admin:" prefix.
MAX_ADMIN_KEY_LENGTH = 122


async def adjust_coins(
    session: AsyncSession, telegram_user_id: int, amount: int, reason: str, key: str | None = None
) -> tuple[coins.Balance, bool]:
    """Grant (positive) or correct (negative) an account's coins.

    Returns the new balance and whether this call applied it. With `key`, running the
    same adjustment again (say, after an unclear failure) applies it once; a key is for
    one adjustment only, and reusing it for a different one is refused. Each
    adjustment is a ledger entry with the reason as its note, and an audit entry. A
    correction cannot take the balance below what is available.
    """
    reason = reason.strip()
    if not reason:
        raise AdminError("Give a reason for the adjustment.")
    if amount == 0:
        raise AdminError("The amount must not be 0.")
    if key is not None:
        key = key.strip()
        if not 1 <= len(key) <= MAX_ADMIN_KEY_LENGTH:
            raise AdminError(f"The key must be 1 to {MAX_ADMIN_KEY_LENGTH} characters.")
    if abs(amount) > MAX_COIN_ADJUSTMENT:
        raise AdminError(
            f"The amount must be between -{MAX_COIN_ADJUSTMENT} and {MAX_COIN_ADJUSTMENT}."
        )
    user = (
        await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    ).scalar_one_or_none()
    if user is None:
        raise AdminError("No account for that Telegram user ID. They must start the bot first.")
    ledger_key = f"admin:{uuid.uuid4() if key is None else key}"
    if amount > 0:
        applied = await coins.credit(
            session, user.id, amount, "admin_grant", ledger_key, note=reason
        )
    else:
        try:
            applied = await coins.debit(
                session, user.id, -amount, "admin_correction", ledger_key, note=reason
            )
        except coins.InsufficientCoinsError as error:
            raise AdminError(
                f"Only {error.available} coins are available; a correction can't go below 0."
            ) from error
    if not applied:
        existing = await coins.recorded(session, ledger_key)
        kind = "admin_grant" if amount > 0 else "admin_correction"
        if existing != coins.Recorded(user.id, amount, kind, reason):
            raise AdminError("That key was already used for a different adjustment.")
    if applied:
        session.add(
            AdminAuditEntry(
                actor_user_id=None,
                action="coins_adjusted",
                target_user_id=user.id,
                new_value=str(amount),
            )
        )
    return await coins.balance(session, user.id), applied


async def coin_balance(session: AsyncSession, telegram_user_id: int) -> coins.Balance:
    user_id = await session.scalar(select(User.id).where(User.telegram_user_id == telegram_user_id))
    if user_id is None:
        raise AdminError("No account for that Telegram user ID. They must start the bot first.")
    return await coins.balance(session, user_id)


async def list_staff(session: AsyncSession) -> list[tuple[int, str, str]]:
    """(Telegram user ID, role, status) of every account with a role other than user."""
    rows = await session.execute(
        select(User.telegram_user_id, User.role, User.status)
        .where(User.role != "user")
        .order_by(User.role, User.telegram_user_id)
    )
    return [(row[0], row[1], row[2]) for row in rows]


# --- Runtime settings ----------------------------------------------------------------


def _out_of_range(key: SettingKey) -> AdminError:
    spec = SPECS[key]
    return AdminError(f"Send a whole number from {spec.minimum} to {spec.maximum} ({spec.unit}).")


def parse_setting_value(key: SettingKey, raw: str) -> int:
    """The whole number in `raw`, if it is within the setting's allowed range."""
    text = raw.strip()
    # The length check comes first: int() refuses very long digit strings.
    if (
        not 0 < len(text) <= 9
        or not text.isascii()
        or not text.isdigit()
        or not SPECS[key].allows(int(text))
    ):
        raise _out_of_range(key)
    return int(text)


async def change_setting(
    session: AsyncSession, actor_user_id: int, key: SettingKey, value: int
) -> int:
    """Set a runtime setting; returns the previous value. Unchanged values are not logged.

    The acting account is re-checked inside this transaction, with its row share-locked:
    a role change (which takes the row's update lock) cannot slip in between the check
    and the write, so a revoked admin's change is refused. A transaction-scoped
    advisory lock per key serialises concurrent changes, so each audit entry records
    the value it actually replaced.
    """
    if not SPECS[key].allows(value):
        raise _out_of_range(key)
    actor = (
        await session.execute(
            select(User.role, User.status)
            .where(User.id == actor_user_id)
            .with_for_update(read=True)
        )
    ).one_or_none()
    if actor is None or actor.role != "admin" or actor.status != "active":
        raise NotAdminError
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"app_settings:{key}"))))
    old = (await load_values(session))[key]
    if old == value:
        return old
    await session.execute(
        insert(AppSetting)
        .values(key=key.value, value=value)
        .on_conflict_do_update(
            index_elements=[AppSetting.key], set_={"value": value, "updated_at": func.now()}
        )
    )
    session.add(
        AdminAuditEntry(
            actor_user_id=actor_user_id,
            action="setting_changed",
            setting_key=key.value,
            old_value=str(old),
            new_value=str(value),
        )
    )
    return old


# --- Purchases (D-065) ------------------------------------------------------------------


async def purchases(session: AsyncSession, telegram_user_id: int) -> list[Purchase]:
    user_id = await session.scalar(select(User.id).where(User.telegram_user_id == telegram_user_id))
    if user_id is None:
        raise AdminError("No account for that Telegram user ID. They must start the bot first.")
    return await payments.purchases(session, user_id)


async def refund_purchase(
    session: AsyncSession, payment_id: int, reason: str, now: datetime
) -> Reversed | None:
    """Take back a paid purchase and have the worker refund its Stars; returns what was
    taken back, or None if it was already refunded or being refunded (so running the
    same refund again changes nothing). Refused if any of its coins were spent."""
    reason = reason.strip()
    if not reason:
        raise AdminError("Give a reason for the refund.")
    try:
        taken = await payments.refund_by_admin(session, payment_id, reason, now)
    except RefundRefusedError as error:
        if error.refusal is RefundRefusal.NOT_PAID:
            return None
        if error.refusal is RefundRefusal.NOT_FOUND:
            raise AdminError("No purchase with that id.") from error
        raise AdminError(
            f"Only {error.available} of the purchase's {error.needed} coins are available, "
            "so it can't be refunded."
        ) from error
    user_id = await session.scalar(
        select(StarsPayment.user_id).where(StarsPayment.id == payment_id)
    )
    session.add(
        AdminAuditEntry(
            actor_user_id=None,
            action="purchase_refunded",
            target_user_id=user_id,
            new_value=str(payment_id),
        )
    )
    return taken


async def record_payment(
    session: AsyncSession, payment: TelegramTransaction, now: datetime
) -> Fulfilled:
    """Record a payment Telegram has that was never recorded (D-068), with the same
    code as a payment confirmation and Telegram's own user, amount, and invoice
    reference. Refused if it is already recorded or matches none of our invoices."""
    try:
        fulfilled = await payments.fulfil(
            session,
            payment.telegram_user_id,
            payment.charge_id,
            payment.payload or "",
            CURRENCY,
            payment.stars,
            now,
        )
    except UnknownPaymentError as error:
        raise AdminError(
            "That payment matches none of our invoices, so it can't be recorded; "
            "handle it manually."
        ) from error
    if fulfilled.outcome == Outcome.ALREADY:
        raise AdminError("That payment is already recorded. Nothing changed.")
    user_id = await session.scalar(
        select(StarsPayment.user_id).where(StarsPayment.id == fulfilled.payment_id)
    )
    session.add(
        AdminAuditEntry(
            actor_user_id=None,
            action="payment_recorded",
            target_user_id=user_id,
            new_value=str(fulfilled.payment_id),
        )
    )
    return fulfilled
