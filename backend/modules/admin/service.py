"""Role and runtime-setting changes, each recorded in the admin audit log (D-034, D-035).

Like the users service, these functions take an AsyncSession and leave committing
to the caller. Roles are changed only by the server-side CLI (`apps/admin_cli.py`);
no bot handler calls `set_role`.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.admin.models import AdminAuditEntry, AppSetting
from backend.modules.admin.settings import SPECS, SettingKey, load_values
from backend.modules.users.models import ROLES, User


class AdminError(Exception):
    """A rejected administrative change. `message` is safe to show to the admin."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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

    A transaction-scoped advisory lock per key serialises concurrent changes, so each
    audit entry records the value it actually replaced.
    """
    if not SPECS[key].allows(value):
        raise _out_of_range(key)
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
