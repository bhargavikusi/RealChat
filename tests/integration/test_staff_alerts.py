"""Alerting moderators and admins to urgent reports, and the daily summary of open
reports (D-071)."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import moderation as bot_moderation
from apps.bot.moderation import StaffAlerts
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.moderation import alerts
from backend.modules.moderation import service as moderation
from backend.modules.moderation.models import StaffAlert
from backend.modules.users.models import User
from tests.support.moderation import ADMIN, MODERATOR, make_conversation, make_report
from tests.support.telegram import RecordingSession, make_bot

Sessions = async_sessionmaker[AsyncSession]
# 09:30 in India (UTC+5:30), after the default summary hour of 09:00.
MORNING = datetime(2026, 11, 22, 4, 0, tzinfo=UTC)
# 08:30 in India, before it.
EARLY = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)


def _job(
    sessions: Sessions, settings: BotSettings, base: str | None = None
) -> tuple[StaffAlerts, RecordingSession]:
    bot, telegram = make_bot()
    configured = settings.model_copy(update={"public_base_url": base})
    return StaffAlerts(bot, sessions, configured, RuntimeSettings(ttl_seconds=0)), telegram


def _texts(telegram: RecordingSession, chat_id: int) -> list[str]:
    return [m.text for m in telegram.sent_to(chat_id) if isinstance(m, SendMessage)]


async def _file_urgent(sessions: Sessions, people: dict[str, int], now: datetime) -> int:
    """Alice reports Bob's chat as "Appears under 18", through the moderation service."""
    conversation = await make_conversation(sessions, people["alice"], people["bob"])
    async with sessions() as session:
        filed = await moderation.file_report(
            session, people["alice"], conversation, "under_18", now, per_day=10
        )
        await session.commit()
        assert filed.report_id is not None
        return filed.report_id


async def _alerts(sessions: Sessions) -> list[tuple[str, int]]:
    async with sessions() as session:
        rows = await session.execute(
            select(StaffAlert.kind, StaffAlert.recipient_user_id).order_by(StaffAlert.id)
        )
        return [(kind, recipient) for kind, recipient in rows.all()]


# --- Urgent reports -----------------------------------------------------------------


async def test_an_urgent_report_alerts_every_moderator_and_admin_once(
    people: dict[str, int], session_factory: Sessions, bot_settings: BotSettings
) -> None:
    report_id = await _file_urgent(session_factory, people, EARLY)

    assert sorted(await _alerts(session_factory)) == sorted(
        [("urgent", people["moderator"]), ("urgent", people["admin"])]
    )
    job, telegram = _job(session_factory, bot_settings, "https://realchat.example")
    assert await job.tick(EARLY) == 2
    for staff in (MODERATOR, ADMIN):
        [message] = telegram.sent_to(staff)
        assert isinstance(message, SendMessage)
        assert message.text == (
            "🚨 Urgent report: Appears under 18 · filed 03:00 UTC.\n\nOpen it in 🛡 Moderation."
        )
        assert isinstance(message.reply_markup, InlineKeyboardMarkup)
        [[button]] = message.reply_markup.inline_keyboard
        assert button.web_app is not None
        assert button.web_app.url == f"https://realchat.example/moderation/?report={report_id}"
    # Sent once: later ticks send nothing more (before the summary hour).
    assert await job.tick(EARLY + timedelta(minutes=10)) == 0


async def test_a_non_urgent_report_alerts_nobody(
    people: dict[str, int], session_factory: Sessions
) -> None:
    conversation = await make_conversation(session_factory, people["alice"], people["bob"])
    async with session_factory() as session:
        await moderation.file_report(session, people["alice"], conversation, "spam", EARLY, 10)
        await session.commit()
    assert await _alerts(session_factory) == []


async def test_alerts_carry_no_content_or_names(
    people: dict[str, int], session_factory: Sessions, bot_settings: BotSettings
) -> None:
    await _file_urgent(session_factory, people, EARLY)
    job, telegram = _job(session_factory, bot_settings)
    await job.tick(EARLY)
    for message in telegram.requests:
        assert isinstance(message, SendMessage)
        for name in ("Night Owl", "Moon Child"):
            assert name not in message.text
        assert message.reply_markup is None  # no public address: no button


async def test_someone_no_longer_staff_gets_no_alert(
    people: dict[str, int], session_factory: Sessions, bot_settings: BotSettings
) -> None:
    await _file_urgent(session_factory, people, EARLY)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.id == people["moderator"]).values(role="user")
        )
        await session.execute(
            update(User).where(User.id == people["admin"]).values(status="suspended")
        )
        await session.commit()
    job, telegram = _job(session_factory, bot_settings)
    assert await job.tick(EARLY) == 0
    assert telegram.requests == []
    async with session_factory() as session:
        unsettled = await session.scalar(
            select(func.count()).select_from(StaffAlert).where(StaffAlert.sent_at.is_(None))
        )
        assert unsettled == 0  # settled without sending, never retried


async def test_a_failed_alert_is_retried_and_a_blocked_bot_given_up(
    people: dict[str, int], session_factory: Sessions, bot_settings: BotSettings
) -> None:
    await _file_urgent(session_factory, people, EARLY)
    job, telegram = _job(session_factory, bot_settings)
    telegram.send_error = TelegramBadRequest(
        method=SendMessage(chat_id=MODERATOR, text="x"), message="Bad Request: try again"
    )
    assert await job.tick(EARLY) == 0
    assert await job.tick(EARLY) == 0  # not due again yet
    telegram.send_error = TelegramForbiddenError(
        method=SendMessage(chat_id=MODERATOR, text="x"), message="Forbidden: bot was blocked"
    )
    assert await job.tick(EARLY + alerts.ALERT_RETRY) == 2  # done with: never deliverable
    assert len(telegram.failed_sends) == 4


async def test_staff_who_join_later_get_no_old_alert(
    people: dict[str, int], session_factory: Sessions
) -> None:
    await _file_urgent(session_factory, people, EARLY)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.id == people["carol"]).values(role="moderator")
        )
        await session.commit()
    assert people["carol"] not in [recipient for _, recipient in await _alerts(session_factory)]


# --- The daily summary --------------------------------------------------------------


async def test_the_summary_is_sent_once_a_day_after_its_hour(
    people: dict[str, int], session_factory: Sessions, bot_settings: BotSettings
) -> None:
    await make_report(session_factory, people["alice"], people["bob"], None)
    await make_report(
        session_factory, people["carol"], people["bob"], None, reason="under_18", urgent=True
    )
    job, telegram = _job(session_factory, bot_settings, "https://realchat.example")

    assert await job.tick(EARLY) == 0  # before 09:00 in India
    assert await job.tick(MORNING) == 2
    for staff in (MODERATOR, ADMIN):
        assert _texts(telegram, staff) == [
            "📋 Open reports: 2 (1 urgent).\n\nReview them in 🛡 Moderation."
        ]
        message = telegram.sent_to(staff)[-1]
        assert isinstance(message.reply_markup, InlineKeyboardMarkup)
        [[button]] = message.reply_markup.inline_keyboard
        assert (
            button.web_app is not None
            and button.web_app.url == "https://realchat.example/moderation/"
        )
    # Once a day: later the same day nothing more; the next day again.
    assert await job.tick(MORNING + timedelta(hours=10)) == 0
    assert await job.tick(MORNING + timedelta(days=1)) == 2


async def test_no_summary_without_open_reports(
    people: dict[str, int], session_factory: Sessions, bot_settings: BotSettings
) -> None:
    await make_report(
        session_factory,
        people["alice"],
        people["bob"],
        None,
        status="closed",
        closed_at=EARLY,
        outcome="no_action",
    )
    job, telegram = _job(session_factory, bot_settings)
    assert await job.tick(MORNING) == 0
    assert telegram.requests == []
    assert await _alerts(session_factory) == []


async def test_the_summary_hour_is_an_admin_setting(
    people: dict[str, int], session_factory: Sessions, bot_settings: BotSettings
) -> None:
    await make_report(session_factory, people["alice"], people["bob"], None)
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.REPORT_SUMMARY_HOUR.value, value=20))
        await session.commit()
    job, _ = _job(session_factory, bot_settings)
    assert await job.tick(MORNING) == 0  # 09:30 in India: not yet
    evening = datetime(2026, 11, 22, 14, 30, tzinfo=UTC)  # 20:00 in India
    assert await job.tick(evening) == 2


@pytest.mark.parametrize(
    ("now", "hour", "due"),
    [
        (datetime(2026, 11, 22, 3, 29, tzinfo=UTC), 9, None),  # 08:59 India
        (datetime(2026, 11, 22, 3, 30, tzinfo=UTC), 9, "2026-11-22"),  # 09:00 India
        (datetime(2026, 11, 22, 18, 30, tzinfo=UTC), 0, "2026-11-23"),  # 00:00 next day
    ],
)
def test_the_summary_day_follows_india_time(now: datetime, hour: int, due: str | None) -> None:
    day = alerts.summary_day(now, hour)
    assert (None if day is None else day.isoformat()) == due


def test_the_summary_text() -> None:
    assert bot_moderation.summary_text(alerts.OpenCounts(3, 0)) == (
        "📋 Open reports: 3.\n\nReview them in 🛡 Moderation."
    )
