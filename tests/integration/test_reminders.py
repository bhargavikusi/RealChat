"""Daily reward reminders (D-060): who, when (rhythm, then country), once a day."""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from redis.asyncio import Redis
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import coins as cn
from apps.bot import reminders as rm
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.reminders import service as reminders
from backend.modules.reminders.models import DailyReminder
from backend.modules.reminders.timezones import country_zone
from backend.modules.users import service as users
from backend.modules.users.models import TermsAcceptance, User
from backend.modules.users.service import Notification
from backend.modules.users.terms import TERMS_VERSION
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.world import buttons, text_of

Sessions = async_sessionmaker[AsyncSession]
TODAY = datetime.now(UTC).date()
# India is UTC+5:30, so 18:00 there is 12:30 UTC.
BEFORE_EVENING = datetime.combine(TODAY, time(12, 0), tzinfo=UTC)
EVENING = datetime.combine(TODAY, time(12, 45), tzinfo=UTC)


@pytest.fixture
async def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    """An onboarded user in India, with the daily reward on."""
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    await set_setting(session_factory, SettingKey.DAILY_REWARD_ON, 1)
    return chat


async def set_setting(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        await session.execute(delete(AppSetting).where(AppSetting.key == key.value))
        session.add(AppSetting(key=key.value, value=value))
        await session.commit()


async def user_id(sessions: Sessions) -> int:
    async with sessions() as session:
        return (await users.get_or_create_user(session, USER_ID)).id


async def no_pause(seconds: float) -> None:
    pass


def job(sessions: Sessions, chat: Chat) -> reminders.ReminderJob:
    return reminders.ReminderJob(
        sessions,
        RuntimeSettings(ttl_seconds=0),
        rm.ReminderMessages(chat.bot),
        TERMS_VERSION,
        pause=no_pause,
    )


def reminders_sent(chat: Chat) -> list[SendMessage]:
    return [
        m
        for m in chat.session.sent_to(USER_ID)
        if isinstance(m, SendMessage) and "daily reward is waiting" in text_of(m)
    ]


async def claim_at(sessions: Sessions, when: datetime) -> None:
    """A daily reward claim recorded at `when` (the ledger keeps its own time)."""
    uid = await user_id(sessions)
    async with sessions() as session:
        await session.execute(
            text(
                "INSERT INTO coin_transactions (user_id, amount, kind, idempotency_key, "
                "created_at) VALUES (:u, 1, 'daily_reward', :k, :t)"
            ),
            {"u": uid, "k": f"daily:{uid}:{when.date().isoformat()}", "t": when},
        )
        await session.commit()


async def reminded(sessions: Sessions, day: date, at: datetime, status: str = "sent") -> None:
    async with sessions() as session:
        session.add(
            DailyReminder(user_id=await user_id(sessions), day=day, status=status, created_at=at)
        )
        await session.commit()


# --- When -------------------------------------------------------------------------------


async def test_without_a_claim_the_countrys_evening_is_used(
    chat: Chat, session_factory: Sessions
) -> None:
    assert country_zone("IN").key == "Asia/Kolkata"
    reminder_job = job(session_factory, chat)
    assert await reminder_job.tick(BEFORE_EVENING) == 0
    assert await reminder_job.tick(EVENING) == 1
    (message,) = reminders_sent(chat)
    assert text_of(message) == rm.REMINDER.format(amount="1 coin")
    assert buttons(message) == [cn.CLAIM_BUTTON, rm.TURN_OFF_BUTTON]
    # Once a day.
    assert await reminder_job.tick(EVENING + timedelta(hours=1)) == 0
    assert len(reminders_sent(chat)) == 1


async def test_the_local_hour_is_an_admin_setting(chat: Chat, session_factory: Sessions) -> None:
    await set_setting(session_factory, SettingKey.REMINDER_LOCAL_HOUR, 17)
    # 17:00 in India is 11:30 UTC.
    assert (
        await job(session_factory, chat).tick(datetime.combine(TODAY, time(11, 45), tzinfo=UTC))
        == 1
    )


async def test_the_users_rhythm_comes_first(chat: Chat, session_factory: Sessions) -> None:
    await claim_at(
        session_factory, datetime.combine(TODAY - timedelta(days=1), time(9, 0), tzinfo=UTC)
    )
    reminder_job = job(session_factory, chat)
    # Their last claim was at 09:00 UTC, well before India's evening.
    assert await reminder_job.tick(datetime.combine(TODAY, time(8, 59), tzinfo=UTC)) == 0
    assert await reminder_job.tick(datetime.combine(TODAY, time(9, 0), tzinfo=UTC)) == 1


async def test_claims_before_an_account_deletion_dont_count(
    chat: Chat, session_factory: Sessions
) -> None:
    await claim_at(
        session_factory, datetime.combine(TODAY - timedelta(days=1), time(9, 0), tzinfo=UTC)
    )
    uid = await user_id(session_factory)
    async with session_factory() as session:
        # As after a deletion: the claim is now before the account's fresh start.
        await session.execute(
            update(User).where(User.id == uid).values(coin_history_after_id=10**9)
        )
        await session.commit()
    # So the country's evening is used, not 09:00.
    assert (
        await job(session_factory, chat).tick(datetime.combine(TODAY, time(9, 0), tzinfo=UTC)) == 0
    )
    assert await job(session_factory, chat).tick(EVENING) == 1


async def test_two_reminders_are_never_close_together(
    chat: Chat, session_factory: Sessions
) -> None:
    # Reminded for yesterday only two hours ago (a local evening spanning midnight UTC).
    await reminded(session_factory, TODAY - timedelta(days=1), EVENING - timedelta(hours=2))
    reminder_job = job(session_factory, chat)
    assert await reminder_job.tick(EVENING) == 0
    # Twenty hours after it, today's reminder goes out.
    async with session_factory() as session:
        await session.execute(
            update(DailyReminder).values(created_at=EVENING - timedelta(hours=20))
        )
        await session.commit()
    assert await reminder_job.tick(EVENING) == 1


# --- Who --------------------------------------------------------------------------------


async def test_no_reminder_with_the_reward_off(chat: Chat, session_factory: Sessions) -> None:
    await set_setting(session_factory, SettingKey.DAILY_REWARD_ON, 0)
    assert await job(session_factory, chat).tick(EVENING) == 0


async def test_no_reminder_with_reminders_off(chat: Chat, session_factory: Sessions) -> None:
    async with session_factory() as session:
        await users.set_notification(
            session, await user_id(session_factory), Notification.REMINDERS, False
        )
        await session.commit()
    assert await job(session_factory, chat).tick(EVENING) == 0


async def test_no_reminder_once_claimed_today(chat: Chat, session_factory: Sessions) -> None:
    await claim_at(session_factory, datetime.combine(TODAY, time(0, 5), tzinfo=UTC))
    assert await job(session_factory, chat).tick(EVENING) == 0


@pytest.mark.parametrize("status", ["suspended", "banned", "deleted"])
async def test_no_reminder_for_accounts_that_cannot_claim(
    chat: Chat, session_factory: Sessions, status: str
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == USER_ID)
            .values(status=status, deleted_at=EVENING if status == "deleted" else None)
        )
        await session.commit()
    assert await job(session_factory, chat).tick(EVENING) == 0


async def test_no_reminder_without_the_current_terms(chat: Chat, session_factory: Sessions) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(TermsAcceptance).where(TermsAcceptance.user_id == await user_id(session_factory))
        )
        await session.commit()
    assert await job(session_factory, chat).tick(EVENING) == 0


async def test_no_reminder_before_onboarding_is_finished(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await chat.send("/start")
    await chat.press("I agree")
    await set_setting(session_factory, SettingKey.DAILY_REWARD_ON, 1)
    # No country yet, so it would be UTC's evening: well past it.
    assert await job(session_factory, chat).tick(datetime.combine(TODAY, time(19), tzinfo=UTC)) == 0


# --- Once, and unreachable users -------------------------------------------------------------


async def test_a_reminder_recorded_before_a_crash_is_not_sent_again(
    chat: Chat, session_factory: Sessions
) -> None:
    await reminded(session_factory, TODAY, EVENING - timedelta(hours=21), status="sending")
    assert await job(session_factory, chat).tick(EVENING) == 0
    assert reminders_sent(chat) == []


async def test_concurrent_runs_send_once(chat: Chat, session_factory: Sessions) -> None:
    first, second = job(session_factory, chat), job(session_factory, chat)
    sent = await asyncio.gather(first.tick(EVENING), second.tick(EVENING))
    assert sum(sent) == 1
    assert len(reminders_sent(chat)) == 1


async def test_someone_who_blocked_the_bot_is_skipped_until_they_return(
    chat: Chat, session_factory: Sessions
) -> None:
    telegram = chat.session
    record = telegram.make_request
    blocked = True

    async def refusing(bot: Any, method: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if blocked and isinstance(method, SendMessage) and "daily reward" in method.text:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        return await record(bot, method, timeout)

    telegram.make_request = refusing  # type: ignore[method-assign]
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == USER_ID)
            .values(last_active_at=EVENING - timedelta(days=3))
        )
        await session.commit()
    reminder_job = job(session_factory, chat)
    assert await reminder_job.tick(EVENING) == 0
    async with session_factory() as session:
        assert await session.scalar(select(DailyReminder.status)) == "unreachable"
    blocked = False
    tomorrow = EVENING + timedelta(days=1)
    assert await reminder_job.tick(tomorrow) == 0  # still skipped
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == USER_ID)
            .values(last_active_at=tomorrow + timedelta(minutes=1))
        )
        await session.commit()
    assert await reminder_job.tick(tomorrow + timedelta(minutes=2)) == 1


async def test_old_reminder_rows_are_cleaned_up(chat: Chat, session_factory: Sessions) -> None:
    await reminded(session_factory, TODAY - timedelta(days=31), EVENING - timedelta(days=31))
    await reminded(session_factory, TODAY - timedelta(days=2), EVENING - timedelta(days=2))
    await job(session_factory, chat).tick(BEFORE_EVENING)
    async with session_factory() as session:
        days = list(await session.scalars(select(DailyReminder.day)))
    assert days == [TODAY - timedelta(days=2)]


async def test_account_deletion_erases_reminders(chat: Chat, session_factory: Sessions) -> None:
    await job(session_factory, chat).tick(EVENING)
    async with session_factory() as session:
        await users.delete_account(session, await user_id(session_factory), datetime.now(UTC))
        await session.commit()
    async with session_factory() as session:
        assert list(await session.scalars(select(DailyReminder.day))) == []


# --- The buttons -----------------------------------------------------------------------------


async def test_the_reminder_buttons_claim_and_turn_reminders_off(
    chat: Chat, session_factory: Sessions
) -> None:
    await job(session_factory, chat).tick(EVENING)
    data = chat.session.last_buttons()
    await chat.press_data(data[cn.CLAIM_BUTTON])
    assert chat.last() == cn.CLAIMED.format(amount=cn.coin_count(1))
    await chat.press_data(data[rm.TURN_OFF_BUTTON])
    async with session_factory() as session:
        choices = await users.notification_settings(session, await user_id(session_factory))
    assert choices[Notification.REMINDERS] is False


def test_main_zones_override_the_table() -> None:
    assert country_zone("RU").key == "Europe/Moscow"
    assert country_zone("US").key == "America/New_York"
    assert country_zone("GB").key == "Europe/London"
    assert country_zone(None).key == "UTC"


async def test_a_run_that_saw_the_user_before_another_recorded_sends_nothing(
    chat: Chat, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_factory() as session:
        seen = await reminders.candidates(session, EVENING, TERMS_VERSION)
        await session.commit()
    # Another run recorded today's reminder meanwhile.
    await reminded(session_factory, TODAY, EVENING - timedelta(hours=21), status="sending")

    async def stale(*args: object, after: int = 0, **kwargs: object) -> list[reminders.Candidate]:
        return [candidate for candidate in seen if candidate.user_id > after]

    monkeypatch.setattr(reminders, "candidates", stale)
    assert await job(session_factory, chat).tick(EVENING) == 0
    assert reminders_sent(chat) == []


async def _stale_candidates(sessions: Sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next run work from candidates read now, as if it read them just before
    whatever the test does next."""
    async with sessions() as session:
        seen = await reminders.candidates(session, EVENING, TERMS_VERSION)
        await session.commit()

    async def stale(*args: object, after: int = 0, **kwargs: object) -> list[reminders.Candidate]:
        return [candidate for candidate in seen if candidate.user_id > after]

    monkeypatch.setattr(reminders, "candidates", stale)


async def test_a_claim_made_while_the_batch_was_prepared_stops_the_reminder(
    chat: Chat, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _stale_candidates(session_factory, monkeypatch)
    await claim_at(session_factory, datetime.combine(TODAY, time(12, 40), tzinfo=UTC))
    assert await job(session_factory, chat).tick(EVENING) == 0
    assert reminders_sent(chat) == []


async def test_an_account_deleted_while_the_batch_was_prepared_is_not_reminded(
    chat: Chat, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _stale_candidates(session_factory, monkeypatch)
    async with session_factory() as session:
        await users.delete_account(session, await user_id(session_factory), datetime.now(UTC))
        await session.commit()
    assert await job(session_factory, chat).tick(EVENING) == 0
    async with session_factory() as session:
        assert list(await session.scalars(select(DailyReminder.day))) == []


async def test_candidates_are_read_in_pages_up_to_the_batch(
    chat: Chat,
    session_factory: Sessions,
    bot_settings: BotSettings,
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for telegram_id in (6464, 7575):
        await complete_onboarding(Chat(bot_settings, redis, session_factory, user_id=telegram_id))
    monkeypatch.setattr(reminders, "PAGE_SIZE", 1)
    monkeypatch.setattr(reminders, "BATCH_SIZE", 2)
    reminder_job = job(session_factory, chat)
    assert await reminder_job.tick(EVENING) == 2
    assert await reminder_job.tick(EVENING) == 1
