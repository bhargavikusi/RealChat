"""Alerts to moderators and admins (D-071): each urgent report as soon as it is filed,
and once a day a summary of open reports.

Like the other services, these take an AsyncSession and leave committing to the
caller. Alerts carry no chat content, report details, or anyone's profile: only the
reason, the time, and counts. Whether someone is still an active moderator or admin is
checked again when their alert is claimed, so a role removed in between stops it.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.moderation.models import Report, StaffAlert
from backend.modules.users.models import User

STAFF_ROLES = ("moderator", "admin")
# The summary hour is India time (owner decision), as are the days it is counted by.
SUMMARY_ZONE = ZoneInfo("Asia/Kolkata")
# As for moderation notices: retried after this long, a few times in all.
ALERT_RETRY = timedelta(minutes=5)
ALERT_ATTEMPTS = 5


async def _active_staff(session: AsyncSession) -> list[int]:
    found = await session.scalars(
        select(User.id).where(User.role.in_(STAFF_ROLES), User.status == "active")
    )
    return list(found.all())


async def queue_urgent(session: AsyncSession, report_id: int, now: datetime) -> int:
    """Owe every active moderator and admin an alert about this urgent report; returns
    how many. Called in the transaction that files the report."""
    staff = await _active_staff(session)
    if not staff:
        return 0
    await session.execute(
        insert(StaffAlert)
        .values(
            [
                {
                    "kind": "urgent",
                    "report_id": report_id,
                    "recipient_user_id": user_id,
                    "next_attempt_at": now,
                }
                for user_id in staff
            ]
        )
        .on_conflict_do_nothing()
    )
    return len(staff)


def summary_day(now: datetime, hour: int) -> date | None:
    """The India date whose summary is due at `now`, or None before today's hour."""
    local = now.astimezone(SUMMARY_ZONE)
    return local.date() if local.hour >= hour else None


async def queue_summary(session: AsyncSession, now: datetime, hour: int) -> int:
    """Once today's summary hour has passed, owe each active moderator and admin
    today's summary, if any report is open; returns how many were added (0 once done
    for today, as each person's summary for a day is unique)."""
    day = summary_day(now, hour)
    if day is None:
        return 0
    if not await session.scalar(
        select(func.count()).select_from(Report).where(Report.status == "open")
    ):
        return 0
    staff = await _active_staff(session)
    if not staff:
        return 0
    added = await session.execute(
        insert(StaffAlert)
        .values(
            [
                {
                    "kind": "summary",
                    "day": day,
                    "recipient_user_id": user_id,
                    "next_attempt_at": now,
                }
                for user_id in staff
            ]
        )
        .on_conflict_do_nothing()
        .returning(StaffAlert.id)
    )
    return len(added.all())


@dataclass(frozen=True)
class DueAlert:
    id: int
    kind: str  # "urgent" or "summary"
    telegram_user_id: int
    report_id: int | None
    reason: str | None
    filed_at: datetime | None


@dataclass(frozen=True)
class OpenCounts:
    open: int
    urgent: int


async def claim(session: AsyncSession, now: datetime, limit: int) -> list[DueAlert]:
    """Claim up to `limit` due alerts, oldest first (SKIP LOCKED, counting an attempt and
    moving the next one ALERT_RETRY ahead). An alert whose recipient is no longer an
    active moderator or admin is settled without sending. The caller commits."""
    due = (
        select(StaffAlert.id)
        .where(
            StaffAlert.sent_at.is_(None),
            StaffAlert.next_attempt_at <= now,
            StaffAlert.attempts < ALERT_ATTEMPTS,
        )
        .order_by(StaffAlert.next_attempt_at, StaffAlert.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = (
        await session.scalars(
            update(StaffAlert)
            .where(StaffAlert.id.in_(due.scalar_subquery()))
            .values(next_attempt_at=now + ALERT_RETRY, attempts=StaffAlert.attempts + 1)
            .returning(StaffAlert.id)
        )
    ).all()
    if not claimed:
        return []
    rows = (
        await session.execute(
            select(
                StaffAlert.id,
                StaffAlert.kind,
                User.telegram_user_id,
                User.role,
                User.status,
                StaffAlert.report_id,
                Report.reason,
                Report.created_at,
            )
            .join(User, User.id == StaffAlert.recipient_user_id)
            .outerjoin(Report, Report.id == StaffAlert.report_id)
            .where(StaffAlert.id.in_(claimed))
            .order_by(StaffAlert.id)
        )
    ).all()
    alerts = []
    for alert_id, kind, telegram_id, role, status, report_id, reason, filed_at in rows:
        if role not in STAFF_ROLES or status != "active":
            await mark_sent(session, alert_id, now)  # no longer staff: settled, not sent
            continue
        alerts.append(DueAlert(alert_id, kind, telegram_id, report_id, reason, filed_at))
    return alerts


async def open_counts(session: AsyncSession) -> OpenCounts:
    """How many reports are open now, and how many of those are urgent."""
    total, urgent = (
        await session.execute(
            select(func.count(), func.count().filter(Report.urgent)).where(Report.status == "open")
        )
    ).one()
    return OpenCounts(int(total), int(urgent))


async def mark_sent(session: AsyncSession, alert_id: int, now: datetime) -> None:
    await session.execute(
        update(StaffAlert)
        .where(StaffAlert.id == alert_id, StaffAlert.sent_at.is_(None))
        .values(sent_at=now)
    )
