"""Moderation analytics for admins (D-072).

Like the other services, these take an AsyncSession and leave committing to the
caller. Days are India calendar days. Reports and moderation actions are kept when
an account is deleted, so the figures include them. An account lookup can't find
(deleted, D-016) is shown without a nickname or RealChat ID and can't be opened.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.analytics.periods import Period, Window, india_day
from backend.modules.moderation.lookup import FINDABLE
from backend.modules.moderation.models import (
    ACTION_KINDS,
    REPORT_REASONS,
    ModerationAction,
    Report,
)
from backend.modules.moderation.review import audit
from backend.modules.users.models import Profile, User

# How many of the most-reported accounts are listed (owner decision).
MOST_REPORTED = 10


@dataclass(frozen=True)
class Bucket:
    start: date  # the first India day in the bucket
    reports: dict[str, int]  # reports made, by reason (every reason, zero included)


@dataclass(frozen=True)
class Backlog:
    """Now, whatever the period."""

    open: int
    urgent: int
    oldest_open_at: datetime | None


@dataclass(frozen=True)
class Handling:
    """Reports closed in the period: from when each was made to when it was closed."""

    closed: int
    action_taken: int
    no_action: int
    median_seconds: float | None
    longest_seconds: float | None


@dataclass(frozen=True)
class Moderator:
    """Actions taken in the period by one member of staff."""

    nickname: str | None  # None once the account can't be found (deleted)
    public_id: str | None
    actions: dict[str, int]  # by kind (every kind, zero included)
    total: int


@dataclass(frozen=True)
class Reported:
    """One of the accounts with the most reports made in the period."""

    id: int
    nickname: str | None  # None once the account can't be found (deleted)
    public_id: str | None
    status: str
    reports: int
    open: int  # of those, still open
    findable: bool  # whether the account view can open it


@dataclass(frozen=True)
class ModerationFigures:
    period: str
    bucket_days: int
    buckets: list[Bucket]
    backlog: Backlog
    handling: Handling
    moderators: list[Moderator]
    most_reported: list[Reported]


async def moderation(
    session: AsyncSession, actor_user_id: int, period: Period, now: datetime
) -> ModerationFigures:
    """The moderation figures for `period`, ending today (India). The view is audited."""
    window = Window.of(period, now)
    figures = ModerationFigures(
        period=period.value,
        bucket_days=window.size,
        buckets=await _buckets(session, window),
        backlog=await _backlog(session),
        handling=await _handling(session, window),
        moderators=await _moderators(session, window),
        most_reported=await _most_reported(session, window),
    )
    session.add(audit(actor_user_id, "analytics_viewed", None, f"moderation {period.value}"))
    return figures


async def _buckets(session: AsyncSession, window: Window) -> list[Bucket]:
    day = india_day(Report.created_at)
    rows = await session.execute(
        select(window.bucket(day), Report.reason, func.count())
        .where(day >= window.first)
        .group_by(literal_column("1"), Report.reason)
    )
    counts: dict[date, dict[str, int]] = {}
    for start, reason, count in rows.all():
        counts.setdefault(start, {})[reason] = int(count)
    return [
        Bucket(start, {reason: counts.get(start, {}).get(reason, 0) for reason in REPORT_REASONS})
        for start in window.starts
    ]


async def _backlog(session: AsyncSession) -> Backlog:
    open_, urgent, oldest = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Report.urgent),
                func.min(Report.created_at),
            ).where(Report.status == "open")
        )
    ).one()
    return Backlog(int(open_), int(urgent), oldest)


async def _handling(session: AsyncSession, window: Window) -> Handling:
    seconds = func.extract("epoch", Report.closed_at - Report.created_at)
    closed, action_taken, no_action, median, longest = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Report.outcome == "action_taken"),
                func.count().filter(Report.outcome == "no_action"),
                func.percentile_cont(0.5).within_group(seconds),
                func.max(seconds),
            ).where(Report.status == "closed", india_day(Report.closed_at) >= window.first)
        )
    ).one()
    return Handling(
        closed=int(closed),
        action_taken=int(action_taken),
        no_action=int(no_action),
        median_seconds=None if median is None else float(median),
        longest_seconds=None if longest is None else float(longest),
    )


async def _moderators(session: AsyncSession, window: Window) -> list[Moderator]:
    rows = await session.execute(
        select(ModerationAction.actor_user_id, ModerationAction.kind, func.count())
        .where(india_day(ModerationAction.created_at) >= window.first)
        .group_by(ModerationAction.actor_user_id, ModerationAction.kind)
    )
    counts: dict[int, dict[str, int]] = {}
    for actor, kind, count in rows.all():
        counts.setdefault(actor, {})[kind] = int(count)
    if not counts:
        return []
    names = {
        row[0]: (row[1], row[2])
        for row in (
            await session.execute(
                select(Profile.user_id, Profile.nickname, Profile.public_id)
                .join(User, User.id == Profile.user_id)
                .where(Profile.user_id.in_(counts), *FINDABLE)
            )
        ).all()
    }
    found = [
        Moderator(
            nickname=names.get(actor, (None, None))[0],
            public_id=names.get(actor, (None, None))[1],
            actions={kind: by_kind.get(kind, 0) for kind in ACTION_KINDS},
            total=sum(by_kind.values()),
        )
        for actor, by_kind in counts.items()
    ]
    return sorted(found, key=lambda item: (-item.total, item.nickname or "￿"))


async def _most_reported(session: AsyncSession, window: Window) -> list[Reported]:
    reports = func.count(Report.id)
    rows = await session.execute(
        select(
            User.id,
            Profile.nickname,
            Profile.public_id,
            User.status,
            reports,
            func.count(Report.id).filter(Report.status == "open"),
            and_(*FINDABLE).is_(True),
        )
        .select_from(Report)
        .join(User, User.id == Report.reported_user_id)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(india_day(Report.created_at) >= window.first)
        .group_by(User.id, Profile.user_id)
        .order_by(reports.desc(), func.max(Report.created_at).desc(), User.id)
        .limit(MOST_REPORTED)
    )
    # A deleted account keeps an empty profile with a new RealChat ID: never shown.
    return [
        Reported(
            id=row[0],
            nickname=row[1] if row[6] else None,
            public_id=row[2] if row[6] else None,
            status=row[3],
            reports=int(row[4]),
            open=int(row[5]),
            findable=bool(row[6]),
        )
        for row in rows.all()
    ]
