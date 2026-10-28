"""Roles, runtime settings, the audit log, the admin CLI, and Admin settings (D-034, D-035)."""

import asyncio
import re
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps import admin_cli
from apps.bot import account as ac
from apps.bot import admin as ad
from apps.bot import onboarding as ob
from apps.bot.menu import HELP_TEXT
from backend.modules.admin import service
from backend.modules.admin.models import AdminAuditEntry, AppSetting
from backend.modules.admin.service import AdminError
from backend.modules.admin.settings import SPECS, RuntimeSettings, SettingKey, load_values
from backend.modules.users import service as users
from backend.modules.users.models import User
from tests.support.chat import USER_ID, Chat, complete_onboarding

Sessions = async_sessionmaker[AsyncSession]
OTHER_ID = 5151
RETENTION = SettingKey.CHAT_RETENTION_DAYS


async def _create(sessions: Sessions, telegram_id: int = USER_ID) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        await session.commit()
        return user.id


async def _set_role(sessions: Sessions, role: str, telegram_id: int = USER_ID) -> None:
    async with sessions() as session:
        await service.set_role(session, telegram_id, role)
        await session.commit()


async def _audit(sessions: Sessions) -> list[tuple[object, ...]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                AdminAuditEntry.actor_user_id,
                AdminAuditEntry.action,
                AdminAuditEntry.target_user_id,
                AdminAuditEntry.setting_key,
                AdminAuditEntry.old_value,
                AdminAuditEntry.new_value,
            ).order_by(AdminAuditEntry.id)
        )
        return [tuple(row) for row in rows]


async def _values(sessions: Sessions) -> dict[SettingKey, int]:
    async with sessions() as session:
        return await load_values(session)


# --- Roles ---------------------------------------------------------------------------


async def test_new_accounts_have_the_user_role(session_factory: Sessions) -> None:
    await _create(session_factory)
    async with session_factory() as session:
        assert await service.get_role(session, USER_ID) == "user"
        assert await service.get_role(session, OTHER_ID) is None


async def test_role_changes_are_audited_and_unchanged_roles_are_not(
    session_factory: Sessions,
) -> None:
    user_id = await _create(session_factory)
    await _set_role(session_factory, "moderator")
    await _set_role(session_factory, "moderator")  # unchanged: not logged
    await _set_role(session_factory, "user")

    assert await _audit(session_factory) == [
        (None, "role_changed", user_id, None, "user", "moderator"),
        (None, "role_changed", user_id, None, "moderator", "user"),
    ]


async def test_roles_need_an_existing_account_and_a_known_role(
    session_factory: Sessions,
) -> None:
    await _create(session_factory)
    async with session_factory() as session:
        with pytest.raises(AdminError, match="must start the bot first"):
            await service.set_role(session, OTHER_ID, "admin")
        with pytest.raises(AdminError, match="Unknown role"):
            await service.set_role(session, USER_ID, "owner")
    assert await _audit(session_factory) == []


async def test_the_database_rejects_unknown_roles(session_factory: Sessions) -> None:
    await _create(session_factory)
    async with session_factory() as session:
        with pytest.raises(DBAPIError) as excinfo:
            await session.execute(update(User).values(role="owner"))
        assert str(excinfo.value.orig.sqlstate) == "23514"  # type: ignore[union-attr]


async def test_concurrent_role_changes_are_audited_in_order(session_factory: Sessions) -> None:
    user_id = await _create(session_factory)
    async with session_factory() as first:
        await service.set_role(first, USER_ID, "moderator")

        async def second() -> None:
            async with session_factory() as session:
                await service.set_role(session, USER_ID, "admin")
                await session.commit()

        task = asyncio.create_task(second())
        await asyncio.sleep(0.2)
        assert not task.done()  # waits for the first transaction's row lock
        await first.commit()
        await task

    assert await _audit(session_factory) == [
        (None, "role_changed", user_id, None, "user", "moderator"),
        (None, "role_changed", user_id, None, "moderator", "admin"),
    ]


def test_no_bot_module_changes_roles() -> None:
    # Roles are changed only by the server CLI (D-034). A bot handler that calls
    # set_role or assigns a role would let a Telegram user grant access.
    pattern = re.compile(r"set_role|\.role\s*=[^=]|role\s*=\s*['\"]|values\([^)]*role")
    offenders = [
        str(path)
        for path in Path("apps/bot").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --- Admin CLI -----------------------------------------------------------------------


async def test_cli_sets_lists_and_revokes_roles(
    session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    await _create(session_factory)

    assert await admin_cli.run(["set-role", str(USER_ID), "admin"], session_factory) == 0
    assert await admin_cli.run(["list"], session_factory) == 0
    assert await admin_cli.run(["revoke", str(USER_ID)], session_factory) == 0
    assert await admin_cli.run(["revoke", str(USER_ID)], session_factory) == 0
    assert await admin_cli.run(["list"], session_factory) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"{USER_ID}: user -> admin",
        f"{USER_ID}\tadmin\tactive",
        f"{USER_ID}: admin -> user",
        f"{USER_ID} already has the user role. Nothing changed.",
        "No moderators or admins.",
    ]


async def test_cli_reports_a_missing_account(
    session_factory: Sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await admin_cli.run(["set-role", str(OTHER_ID), "admin"], session_factory) == 1
    assert "must start the bot first" in capsys.readouterr().err
    assert await _audit(session_factory) == []


async def test_cli_rejects_unknown_roles(session_factory: Sessions) -> None:
    with pytest.raises(SystemExit):
        await admin_cli.run(["set-role", str(USER_ID), "owner"], session_factory)


# --- Runtime settings ----------------------------------------------------------------


async def test_defaults_apply_until_a_setting_is_changed(session_factory: Sessions) -> None:
    assert await _values(session_factory) == {key: spec.default for key, spec in SPECS.items()}


async def test_setting_changes_are_audited_and_unchanged_values_are_not(
    session_factory: Sessions,
) -> None:
    admin_id = await _create(session_factory)
    async with session_factory() as session:
        assert await service.change_setting(session, admin_id, RETENTION, 14) == 7
        assert await service.change_setting(session, admin_id, RETENTION, 14) == 14
        assert await service.change_setting(session, admin_id, RETENTION, 30) == 14
        await session.commit()

    assert (await _values(session_factory))[RETENTION] == 30
    assert await _audit(session_factory) == [
        (admin_id, "setting_changed", None, RETENTION.value, "7", "14"),
        (admin_id, "setting_changed", None, RETENTION.value, "14", "30"),
    ]


async def test_out_of_range_changes_are_rejected(session_factory: Sessions) -> None:
    admin_id = await _create(session_factory)
    async with session_factory() as session:
        with pytest.raises(AdminError):
            await service.change_setting(session, admin_id, RETENTION, 0)
    assert (await _values(session_factory))[RETENTION] == 7
    assert await _audit(session_factory) == []


async def test_out_of_range_stored_values_fall_back_to_the_default(
    session_factory: Sessions,
) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=RETENTION.value, value=10_000))
        session.add(AppSetting(key="retired_setting", value=1))
        await session.commit()
    assert (await _values(session_factory))[RETENTION] == 7


async def test_concurrent_setting_changes_are_audited_in_order(
    session_factory: Sessions,
) -> None:
    admin_id = await _create(session_factory)
    async with session_factory() as first:
        await service.change_setting(first, admin_id, RETENTION, 14)

        async def second() -> None:
            async with session_factory() as session:
                await service.change_setting(session, admin_id, RETENTION, 30)
                await session.commit()

        task = asyncio.create_task(second())
        await asyncio.sleep(0.2)
        assert not task.done()  # waits for the first transaction's lock
        await first.commit()
        await task

    assert [entry[4:] for entry in await _audit(session_factory)] == [("7", "14"), ("14", "30")]


async def test_runtime_settings_cache_expires_and_can_be_invalidated(
    session_factory: Sessions,
) -> None:
    admin_id = await _create(session_factory)
    now = [0.0]
    cached = RuntimeSettings(ttl_seconds=10, clock=lambda: now[0])

    async def change(value: int) -> None:
        async with session_factory() as session:
            await service.change_setting(session, admin_id, RETENTION, value)
            await session.commit()

    async with session_factory() as session:
        assert await cached.get(session, RETENTION) == 7
        await change(14)
        now[0] = 9.9
        assert await cached.get(session, RETENTION) == 7  # still cached
        now[0] = 10.0
        assert await cached.get(session, RETENTION) == 14  # another process sees it in time
        await change(30)
        cached.invalidate()
        assert await cached.get(session, RETENTION) == 30  # the changing process sees it at once


# --- Admin settings in the bot -------------------------------------------------------


async def _open_advanced_settings(chat: Chat) -> dict[str, str]:
    await chat.send("/profile")
    assert await chat.press(ac.ADVANCED_SETTINGS) == ac.ADVANCED_SETTINGS_TEXT
    return chat.session.last_buttons()


async def test_only_admins_see_admin_settings(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    assert ad.ADMIN_BUTTON not in await _open_advanced_settings(chat)
    await _set_role(session_factory, "moderator")
    assert ad.ADMIN_BUTTON not in await _open_advanced_settings(chat)
    await _set_role(session_factory, "admin")
    assert ad.ADMIN_BUTTON in await _open_advanced_settings(chat)


async def test_admin_changes_a_setting(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    await _set_role(session_factory, "admin")
    runtime: RuntimeSettings = chat.dispatcher["runtime_settings"]
    async with session_factory() as session:
        assert await runtime.get(session, RETENTION) == 7  # now cached for 10 seconds

    await _open_advanced_settings(chat)
    assert await chat.press(ad.ADMIN_BUTTON) == ad.overview(await _values(session_factory))
    assert await chat.press(SPECS[RETENTION].label) == ad.edit_prompt(RETENTION, 7)

    assert await chat.send("abc") == "Send a whole number from 1 to 365 (days)."
    assert await chat.send("/help") == HELP_TEXT  # commands are never taken as values
    assert (await _values(session_factory))[RETENTION] == 7

    reply = await chat.send("14")
    values = await _values(session_factory)
    assert values[RETENTION] == 14
    assert reply == f"✅ Chat message retention is now 14 days (was 7).\n\n{ad.overview(values)}"
    async with session_factory() as session:
        assert await runtime.get(session, RETENTION) == 14  # applied without a restart

    admin_id = (await _audit(session_factory))[-1][0]
    assert admin_id is not None
    assert (await _audit(session_factory))[-1] == (
        admin_id,
        "setting_changed",
        None,
        RETENTION.value,
        "7",
        "14",
    )


async def test_cancelled_and_old_edits_change_nothing(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await _set_role(session_factory, "admin")
    await _open_advanced_settings(chat)
    await chat.press(ad.ADMIN_BUTTON)
    await chat.press(SPECS[RETENTION].label)
    old_cancel = chat.session.last_buttons()[ad.CANCEL_BUTTON]

    await chat.press_data(ad.AdminAction(action="edit", key=RETENTION.value).pack())
    await chat.press_data(old_cancel)
    assert chat.session.callback_answers()[-1] == ad.EXPIRED

    assert await chat.press(ad.CANCEL_BUTTON) == ad.CANCELLED
    assert await chat.send("14") == ob.WELCOME_BACK  # no longer an answer
    assert (await _values(session_factory))[RETENTION] == 7
    assert await _audit(session_factory) == [
        (None, "role_changed", 1, None, "user", "admin"),
    ]


@pytest.mark.parametrize("role", ["user", "moderator"])
async def test_non_admins_cannot_use_admin_buttons(
    chat: Chat, session_factory: Sessions, role: str
) -> None:
    await complete_onboarding(chat)
    await _set_role(session_factory, role)
    for data in (
        ad.AdminAction(action="open").pack(),
        ad.AdminAction(action="edit", key=RETENTION.value).pack(),
    ):
        sent = len(chat.session.sent_texts())
        await chat.press_data(data)
        assert chat.session.sent_texts()[sent:] == []
        assert chat.session.callback_answers()[-1] == ad.EXPIRED
    assert await chat.send("14") == ob.WELCOME_BACK


async def test_unknown_setting_buttons_are_expired(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    await _set_role(session_factory, "admin")
    await chat.press_data(ad.AdminAction(action="edit", key="retired_setting").pack())
    assert chat.session.callback_answers()[-1] == ad.EXPIRED


async def test_a_revoked_admin_cannot_finish_an_edit(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    await _set_role(session_factory, "admin")
    await _open_advanced_settings(chat)
    await chat.press(ad.ADMIN_BUTTON)
    await chat.press(SPECS[RETENTION].label)
    await _set_role(session_factory, "user")

    assert await chat.send("14") == ad.NOT_AVAILABLE
    assert (await _values(session_factory))[RETENTION] == 7


async def test_restricted_admins_are_stopped_by_the_gate(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await _set_role(session_factory, "admin")
    async with session_factory() as session:
        await session.execute(text("UPDATE users SET status = 'suspended'"))
        await session.commit()

    await chat.press_data(ad.AdminAction(action="open").pack())
    assert chat.last() == ac.restriction_notice("suspended")
