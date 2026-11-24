"""Growth and activity analytics for admins (D-072).

Like the other services, these take an AsyncSession and leave committing to the
caller. Every figure is a total: no individual person is returned. Days are India
calendar days. Finished onboardings count only accounts that still exist, since
deletion erases the profile. Active users come from `user_activity_days`, recorded
from when that table was added.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import Date, Integer, cast, delete, func, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from backend.modules.moderation.review import audit
from backend.modules.payments.models import StarsInvoice, StarsPayment
from backend.modules.users.models import Profile, User, UserActivityDay

logger = logging.getLogger(__name__)

ZONE = ZoneInfo("Asia/Kolkata")
# How long the days a user was active are kept (owner decision).
ACTIVITY_DAYS_KEPT = 400


class Period(StrEnum):
    WEEK = "7d"
    MONTH = "30d"
    QUARTER = "90d"
    YEAR = "12m"


# Days covered, and the bucket size in days (a year is shown by week).
SPANS = {Period.WEEK: (7, 1), Period.MONTH: (30, 1), Period.QUARTER: (90, 1), Period.YEAR: (364, 7)}


@dataclass(frozen=True)
class Bucket:
    start: date  # the first India day in the bucket
    signups: int
    onboarded: int
    vip_purchases: int
    active: int  # distinct users active in the bucket
    active_7d: int  # distinct users active in the 7 days ending with the bucket
    active_30d: int  # distinct users active in the 30 days ending with the bucket


@dataclass(frozen=True)
class Totals:
    by_status: dict[str, int]
    onboarded: int
    vip_now: int
    active_today: int
    active_7d: int
    active_30d: int


@dataclass(frozen=True)
class Growth:
    period: str
    bucket_days: int
    buckets: list[Bucket]
    totals: Totals


def _india_day(
    column: InstrumentedAttribute[datetime] | InstrumentedAttribute[datetime | None],
) -> ColumnElement[date]:
    """A timestamp column as its India calendar date."""
    return cast(func.timezone("Asia/Kolkata", column), Date)


async def _per_bucket(
    session: AsyncSession,
    day: ColumnElement[date],
    *where: ColumnElement[bool],
    first: date,
    size: int,
) -> dict[date, int]:
    """Counts by bucket start for rows whose India day falls in the period. In
    PostgreSQL, a date minus a date is a number of days, and a date plus a number of
    days is a date."""
    start = literal(first, Date)
    bucket = start + cast(day - start, Integer) // size * size
    rows = await session.execute(
        select(cast(bucket, Date), func.count())
        .where(day >= start, *where)
        .group_by(literal_column("1"))
    )
    return {row[0]: int(row[1]) for row in rows.all()}


async def growth(
    session: AsyncSession, actor_user_id: int, period: Period, now: datetime
) -> Growth:
    """The growth figures for `period`, ending today (India). The view is audited."""
    today = now.astimezone(ZONE).date()
    days, size = SPANS[period]
    first = today - timedelta(days=days - 1)
    starts = [first + timedelta(days=offset) for offset in range(0, days, size)]

    signups = await _per_bucket(session, _india_day(User.created_at), first=first, size=size)
    onboarded = await _per_bucket(
        session,
        _india_day(Profile.onboarding_completed_at),
        Profile.onboarding_completed_at.is_not(None),
        first=first,
        size=size,
    )
    vip = await _per_bucket(
        session,
        _india_day(StarsPayment.created_at),
        StarsPayment.status == "paid",
        StarsPayment.invoice_id.in_(select(StarsInvoice.id).where(StarsInvoice.product == "vip")),
        first=first,
        size=size,
    )
    active = await _active(session, starts, size)

    buckets = [
        Bucket(
            start=start,
            signups=signups.get(start, 0),
            onboarded=onboarded.get(start, 0),
            vip_purchases=vip.get(start, 0),
            active=active[start][0],
            active_7d=active[start][1],
            active_30d=active[start][2],
        )
        for start in starts
    ]
    session.add(audit(actor_user_id, "analytics_viewed", None, f"growth {period.value}"))
    return Growth(period.value, size, buckets, await _totals(session, today, now))


async def _active(
    session: AsyncSession, starts: list[date], size: int
) -> dict[date, tuple[int, int, int]]:
    """For each bucket: distinct users active in it, and in the 7 and 30 days ending
    with its last day."""
    series = (
        select(
            func.generate_series(starts[0], starts[-1], timedelta(days=size))
            .cast(Date)
            .label("start")
        )
    ).subquery()
    end = series.c.start + (size - 1)
    rows = await session.execute(
        select(
            series.c.start,
            func.count(func.distinct(UserActivityDay.user_id)).filter(
                UserActivityDay.day >= series.c.start, UserActivityDay.day <= end
            ),
            func.count(func.distinct(UserActivityDay.user_id)).filter(
                UserActivityDay.day > end - 7, UserActivityDay.day <= end
            ),
            func.count(func.distinct(UserActivityDay.user_id)).filter(
                UserActivityDay.day > end - 30, UserActivityDay.day <= end
            ),
        )
        .select_from(series)
        .outerjoin(
            UserActivityDay,
            (UserActivityDay.day > end - 30) & (UserActivityDay.day <= end),
        )
        .group_by(series.c.start)
    )
    return {row[0]: (int(row[1]), int(row[2]), int(row[3])) for row in rows.all()}


async def _totals(session: AsyncSession, today: date, now: datetime) -> Totals:
    by_status = dict(
        (await session.execute(select(User.status, func.count()).group_by(User.status))).all()
    )
    onboarded = await session.scalar(
        select(func.count())
        .select_from(Profile)
        .join(User, User.id == Profile.user_id)
        .where(Profile.onboarding_completed_at.is_not(None), User.status != "deleted")
    )
    vip_now = await session.scalar(
        select(func.count()).select_from(User).where(User.vip_until > now)
    )
    active_today, active_7d, active_30d = (
        await session.execute(
            select(
                func.count(func.distinct(UserActivityDay.user_id)).filter(
                    UserActivityDay.day == today
                ),
                func.count(func.distinct(UserActivityDay.user_id)).filter(
                    UserActivityDay.day > today - timedelta(days=7)
                ),
                func.count(func.distinct(UserActivityDay.user_id)),
            ).where(UserActivityDay.day > today - timedelta(days=30))
        )
    ).one()
    return Totals(
        by_status={status: int(count) for status, count in by_status.items()},
        onboarded=int(onboarded or 0),
        vip_now=int(vip_now or 0),
        active_today=int(active_today),
        active_7d=int(active_7d),
        active_30d=int(active_30d),
    )


# --- Retention ----------------------------------------------------------------------

RUN_KEY = "retention:user_activity_days:last_run"
RUN_INTERVAL = timedelta(days=1)
RETRY_INTERVAL = timedelta(hours=1)
BATCH_SIZE = 5_000


async def delete_old_activity(session: AsyncSession, before: date, limit: int) -> int:
    """Delete up to `limit` activity days older than `before`; returns how many."""
    old = (
        select(UserActivityDay.user_id, UserActivityDay.day)
        .where(UserActivityDay.day < before)
        .limit(limit)
        .subquery()
    )
    result = await session.execute(
        delete(UserActivityDay).where(
            (UserActivityDay.user_id == old.c.user_id) & (UserActivityDay.day == old.c.day)
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


class ActivityRetention:
    """Deletes activity days older than ACTIVITY_DAYS_KEPT, once a day, like the other
    retention jobs (D-046, D-053)."""

    def __init__(self, redis: Redis, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._redis = redis
        self._sessions = sessions

    async def tick(self, now: datetime) -> int | None:
        due = await self._redis.set(
            RUN_KEY, now.isoformat(), nx=True, ex=int(RETRY_INTERVAL.total_seconds())
        )
        if not due:
            return None
        deleted = await self.run(now)
        await self._redis.set(RUN_KEY, now.isoformat(), ex=int(RUN_INTERVAL.total_seconds()))
        return deleted

    async def run(self, now: datetime) -> int:
        before = now.astimezone(ZONE).date() - timedelta(days=ACTIVITY_DAYS_KEPT)
        total = 0
        while True:
            async with self._sessions() as session:
                deleted = await delete_old_activity(session, before, BATCH_SIZE)
                await session.commit()
            total += deleted
            if deleted < BATCH_SIZE:
                break
            # Still going: keep this run's hold, as the other retention jobs do, so a
            # long cleanup isn't started again by another worker meanwhile.
            await self._redis.set(RUN_KEY, now.isoformat(), ex=int(RETRY_INTERVAL.total_seconds()))
        logger.info("deleted old activity days", extra={"count": total})
        return total
