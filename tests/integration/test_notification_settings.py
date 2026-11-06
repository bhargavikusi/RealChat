"""Notification settings in Advanced settings (D-052)."""

from datetime import UTC, datetime

import pytest
from aiogram.methods import EditMessageReplyMarkup
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import admin as ad
from apps.bot import onboarding as ob
from apps.bot import profile as pr
from backend.config.settings import BotSettings
from backend.modules.admin import service as admin_service
from backend.modules.users import service as users
from backend.modules.users.models import NotificationSettings, User
from backend.modules.users.service import NOTIFICATION_DEFAULTS, Notification
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding
from tests.support.world import buttons, last_to, text_of

Sessions = async_sessionmaker[AsyncSession]
DEFAULT_BUTTONS = [
    "🤫 Silent mode: Off",
    "👀 Visit alerts: On",
    "❤️ Like alerts: On",
    "⏰ Reminders: On",
    ac.DELETE_BUTTON,
    ac.BACK_BUTTON,
]


async def open_settings(chat: Chat) -> None:
    await chat.send("/profile")
    assert await chat.press(ac.ADVANCED_SETTINGS) == ac.ADVANCED_SETTINGS_TEXT


async def choices(sessions: Sessions) -> dict[Notification, bool]:
    async with sessions() as session:
        user = await users.get_or_create_user(session, USER_ID)
        return (await users.notification_settings(session, user.id)).values


def edited_buttons(chat: Chat) -> list[str]:
    edits = [m for m in chat.session.requests if isinstance(m, EditMessageReplyMarkup)]
    markup = edits[-1].reply_markup
    assert markup is not None
    return [button.text for row in markup.inline_keyboard for button in row]


def notify_data(setting: Notification, on: bool) -> str:
    return ac.NotifyAction(setting=setting.value, on=on).pack()


async def test_defaults_without_any_change(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    await open_settings(chat)
    assert buttons(last_to(chat.session, USER_ID)) == DEFAULT_BUTTONS
    assert await choices(session_factory) == {
        Notification.SILENT_MODE: False,
        Notification.VISIT_ALERTS: True,
        Notification.LIKE_ALERTS: True,
        Notification.REMINDERS: True,
    }
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationSettings)) == 0


async def test_admins_also_see_admin_settings(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    async with session_factory() as session:
        await admin_service.set_role(session, USER_ID, "admin")
        await session.commit()
    await open_settings(chat)
    shown = buttons(last_to(chat.session, USER_ID))
    assert shown[-2:] == [ad.ADMIN_BUTTON, ac.BACK_BUTTON]


@pytest.mark.parametrize("setting", list(Notification))
async def test_each_setting_changes_with_one_tap_and_back(
    chat: Chat, session_factory: Sessions, setting: Notification
) -> None:
    await complete_onboarding(chat)
    await open_settings(chat)
    default = NOTIFICATION_DEFAULTS[setting]
    label = f"{ac.NOTIFICATION_LABELS[setting]}: {'On' if default else 'Off'}"
    flipped = f"{ac.NOTIFICATION_LABELS[setting]}: {'Off' if default else 'On'}"
    await chat.press(label)
    assert chat.session.callback_answers()[-1] == ac.changed(setting, not default)
    assert (await choices(session_factory))[setting] is (not default)
    assert flipped in edited_buttons(chat)
    others = {s: v for s, v in (await choices(session_factory)).items() if s is not setting}
    assert others == {s: v for s, v in NOTIFICATION_DEFAULTS.items() if s is not setting}
    await chat.press_data(notify_data(setting, default))
    assert (await choices(session_factory))[setting] is default


async def test_silent_mode_explains_itself(chat: Chat) -> None:
    await complete_onboarding(chat)
    await open_settings(chat)
    await chat.press("🤫 Silent mode: Off")
    assert chat.session.callback_answers()[-1] == ac.SILENT_MODE_ON
    await chat.press_data(notify_data(Notification.SILENT_MODE, False))
    assert chat.session.callback_answers()[-1] == ac.SILENT_MODE_OFF


async def test_a_repeated_or_old_tap_never_flips_back(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await open_settings(chat)
    tap = chat.session.last_buttons()["⏰ Reminders: On"]
    await chat.press_data(tap)
    await chat.press_data(tap)
    await chat.press_data(tap)
    assert (await choices(session_factory))[Notification.REMINDERS] is False


async def test_settings_are_only_ever_the_tapping_users_own(
    chat: Chat, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    await complete_onboarding(chat)
    other = Chat(bot_settings, redis, session_factory, user_id=7777)
    await complete_onboarding(other)
    await other.press_data(notify_data(Notification.SILENT_MODE, True))
    assert (await choices(session_factory))[Notification.SILENT_MODE] is False
    async with session_factory() as session:
        other_id = (await users.get_or_create_user(session, 7777)).id
        assert (await users.notification_settings(session, other_id))[Notification.SILENT_MODE]


async def test_an_unknown_setting_is_answered_as_expired(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.press_data(ac.NotifyAction(setting="role", on=True).pack())
    assert chat.session.callback_answers()[-1] == ac.EXPIRED
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationSettings)) == 0


async def test_settings_need_the_current_terms(
    chat: Chat, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    await complete_onboarding(chat)
    monkeypatch.setattr(ob, "TERMS_VERSION", "draft-99")
    sent = len(chat.session.sent_texts())
    await chat.press_data(notify_data(Notification.SILENT_MODE, True))
    assert chat.session.sent_texts()[sent:] == [TERMS]
    assert (await choices(session_factory))[Notification.SILENT_MODE] is False


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_restricted_users_cannot_change_settings(
    chat: Chat, session_factory: Sessions, status: str
) -> None:
    await complete_onboarding(chat)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status=status)
        )
        await session.commit()
    await chat.press_data(notify_data(Notification.SILENT_MODE, True))
    await chat.press_data(ac.BackToProfile().pack())
    assert (await choices(session_factory))[Notification.SILENT_MODE] is False
    assert chat.last().startswith(ac.NOTICES[status])


async def test_account_deletion_resets_the_settings(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    await chat.press_data(notify_data(Notification.SILENT_MODE, True))
    await chat.press_data(notify_data(Notification.REMINDERS, False))
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await users.delete_account(session, user.id, datetime.now(UTC))
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(NotificationSettings)) == 0
    assert await choices(session_factory) == NOTIFICATION_DEFAULTS


async def test_back_shows_the_profile_card(chat: Chat) -> None:
    await complete_onboarding(chat)
    await open_settings(chat)
    await chat.press(ac.BACK_BUTTON)
    card = last_to(chat.session, USER_ID)
    assert text_of(card).startswith(pr.PROFILE_HEADER)
    assert ac.ADVANCED_SETTINGS in buttons(card)
