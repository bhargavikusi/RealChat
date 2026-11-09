"""Daily reward reminders, sent by the worker (D-060).

A user is reminded while the daily reward is on, when they have ⏰ Reminders on (D-052),
can claim it (active, onboarded, current terms), and haven't claimed it this UTC day.

The time follows the user's rhythm: the time of day of their latest daily reward claim
(since their latest account deletion). Without one, it is a local hour (an admin
setting) in their country's main time zone. At most one reminder per user per UTC day,
and at least `MIN_GAP` between two, so a local evening that spans midnight UTC never
gets two. The reminder is recorded before it is sent, so a restart never sends it
twice. A user the last reminder couldn't reach (they blocked the bot) is skipped until
they use RealChat again. Reminders go out in bounded batches, paced toward Telegram.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, exists, func, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.coins.models import CoinTransaction
from backend.modules.reminders.models import DailyReminder
from backend.modules.reminders.timezones import country_zone
from backend.modules.users.models import NotificationSettings, Profile, TermsAcceptance, User

logger = logging.getLogger(__name__)

# Two reminders are never closer than this, whatever the UTC days.
MIN_GAP = timedelta(hours=20)
# Reminder rows are kept this long, then deleted.
KEEP_FOR = timedelta(days=30)
BATCH_SIZE = 100
# Between two sends: well under Telegram's limit of about 30 messages a second.
PAUSE_SECONDS = 0.05


@dataclass(frozen=True)
class Candidate:
    user_id: int
    telegram_id: int
    country_code: str | None
    last_active_at: datetime
    last_claim_at: datetime | None  # the latest daily reward claim, if any
    last_reminder_at: datetime | None
    last_reminder_status: str | None


def _start_of_day(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), tzinfo=UTC)


async def candidates(session: AsyncSession, now: datetime, terms_version: str) -> list[Candidate]:
    """Users who could be reminded today, before timing: eligible, Reminders on, today's
    reward not claimed, and not yet reminded today."""
    after = func.coalesce(User.coin_history_after_id, 0)
    claims = select(CoinTransaction.created_at).where(
        CoinTransaction.user_id == User.id,
        CoinTransaction.kind == "daily_reward",
        CoinTransaction.id > after,
    )
    last_claim = claims.order_by(CoinTransaction.id.desc()).limit(1).scalar_subquery()
    claimed_today = exists(claims.where(CoinTransaction.created_at >= _start_of_day(now)))
    last_reminder = (
        select(DailyReminder.created_at, DailyReminder.status)
        .where(DailyReminder.user_id == User.id)
        .order_by(DailyReminder.day.desc())
        .limit(1)
        .lateral("last_reminder")
    )
    reminded_today = exists().where(
        DailyReminder.user_id == User.id, DailyReminder.day == now.astimezone(UTC).date()
    )
    accepted = exists().where(
        TermsAcceptance.user_id == User.id, TermsAcceptance.terms_version == terms_version
    )
    reminders_on = func.coalesce(
        select(NotificationSettings.reminders)
        .where(NotificationSettings.user_id == User.id)
        .scalar_subquery(),
        true(),
    )
    rows = await session.execute(
        select(
            User.id,
            User.telegram_user_id,
            Profile.country_code,
            User.last_active_at,
            last_claim,
            last_reminder.c.created_at,
            last_reminder.c.status,
        )
        .join(Profile, Profile.user_id == User.id)
        .outerjoin(last_reminder, true())
        .where(
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
            accepted,
            reminders_on,
            ~claimed_today,
            ~reminded_today,
        )
        .order_by(User.id)
    )
    return [Candidate(*row) for row in rows]


def is_due(candidate: Candidate, now: datetime, local_hour: int) -> bool:
    """Whether the candidate's reminder time has come today."""
    if candidate.last_reminder_at is not None:
        if now - candidate.last_reminder_at < MIN_GAP:
            return False
        if (
            candidate.last_reminder_status == "unreachable"
            and candidate.last_active_at <= candidate.last_reminder_at
        ):
            return False  # they blocked the bot and haven't come back since
    if candidate.last_claim_at is not None:
        # Their rhythm: the time of day they last claimed, in UTC like the reward's day.
        claim_time = candidate.last_claim_at.astimezone(UTC).time()
        return now.astimezone(UTC).time() >= claim_time
    local = now.astimezone(country_zone(candidate.country_code))
    return local.hour >= local_hour


async def claim_day(session: AsyncSession, user_id: int, now: datetime) -> bool:
    """Record today's reminder, at `now`, before it is sent; False if one already was."""
    recorded = await session.scalar(
        insert(DailyReminder)
        .values(user_id=user_id, day=now.astimezone(UTC).date(), created_at=now)
        .on_conflict_do_nothing()
        .returning(DailyReminder.user_id)
    )
    return recorded is not None


class ReminderSender(Protocol):
    async def remind(self, telegram_id: int, amount: int) -> str:
        """Send the reminder; returns "sent", "unreachable", or "failed"."""
        ...


class ReminderJob:
    """Sends due daily reward reminders, once a minute or so (D-060)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
        sender: ReminderSender,
        terms_version: str,
        pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime_settings
        self._sender = sender
        self._terms_version = terms_version
        self._pause = pause

    async def tick(self, now: datetime) -> int:
        """Returns how many reminders were sent."""
        today = now.astimezone(UTC).date()
        async with self._sessions() as session:
            await session.execute(delete(DailyReminder).where(DailyReminder.day < today - KEEP_FOR))
            on = await self._runtime.get(session, SettingKey.DAILY_REWARD_ON)
            amount = await self._runtime.get(session, SettingKey.DAILY_REWARD_COINS)
            hour = await self._runtime.get(session, SettingKey.REMINDER_LOCAL_HOUR)
            found = await candidates(session, now, self._terms_version) if on else []
            await session.commit()
        due = [candidate for candidate in found if is_due(candidate, now, hour)][:BATCH_SIZE]
        sent = 0
        for candidate in due:
            async with self._sessions() as session:
                claimed = await claim_day(session, candidate.user_id, now)
                await session.commit()
            if not claimed:
                continue
            status = await self._sender.remind(candidate.telegram_id, amount)
            async with self._sessions() as session:
                await session.execute(
                    update(DailyReminder)
                    .where(DailyReminder.user_id == candidate.user_id, DailyReminder.day == today)
                    .values(status=status)
                )
                await session.commit()
            sent += status == "sent"
            await self._pause(PAUSE_SECONDS)
        if due:
            logger.info("sent daily reward reminders", extra={"count": sent, "due": len(due)})
        return sent
