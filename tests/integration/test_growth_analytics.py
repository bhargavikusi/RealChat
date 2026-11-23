"""Growth and activity analytics for admins, and the daily activity they count (D-072)."""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.db import ACTIVITY_INTERVAL
from backend.modules.admin.models import AdminAuditEntry
from backend.modules.analytics import growth
from backend.modules.analytics.growth import ActivityRetention, Period
from backend.modules.payments.models import StarsInvoice, StarsPayment
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User, UserActivityDay
from tests.support.moderation import ADMIN, ALICE, BOB, MODERATOR, Panel
from tests.support.webapp import authorization

Sessions = async_sessionmaker[AsyncSession]
# 12:00 India time on 22 November 2026.
NOON = datetime(2026, 11, 22, 6, 30, tzinfo=UTC)
TODAY = date(2026, 11, 22)


def _get(panel: Panel, as_user: int = ADMIN, **params: Any) -> Any:
    return panel.client.get(
        "/api/moderation/analytics/growth",
        params=params,
        headers=authorization(as_user, datetime.now(UTC)),
    )


async def _active_days(sessions: Sessions, user_id: int, *days: date) -> None:
    async with sessions() as session:
        session.add_all([UserActivityDay(user_id=user_id, day=day) for day in days])
        await session.commit()


# --- Recording activity --------------------------------------------------------------


async def test_activity_is_recorded_once_a_day_in_india_time(
    people: dict[str, int], session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await session.execute(update(User).values(last_active_at=NOON - timedelta(days=2)))
        await session.commit()
    # 23:59 and 00:01 India time on either side of midnight, then again later that day.
    before_midnight = datetime(2026, 11, 22, 18, 29, tzinfo=UTC)
    after_midnight = before_midnight + ACTIVITY_INTERVAL + timedelta(minutes=1)
    for moment in (NOON, before_midnight, after_midnight, after_midnight + timedelta(hours=2)):
        async with session_factory() as session:
            await users.record_activity(session, ALICE, moment, ACTIVITY_INTERVAL)
            await session.commit()
    async with session_factory() as session:
        days = (
            await session.scalars(
                select(UserActivityDay.day)
                .where(UserActivityDay.user_id == people["alice"])
                .order_by(UserActivityDay.day)
            )
        ).all()
    assert days == [TODAY, TODAY + timedelta(days=1)]


async def test_restricted_accounts_record_no_activity(
    people: dict[str, int], session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == people["bob"])
            .values(status="banned", last_active_at=NOON - timedelta(days=1))
        )
        await session.commit()
        await users.record_activity(session, BOB, NOON, ACTIVITY_INTERVAL)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(UserActivityDay)) == 0


async def test_deleting_an_account_erases_its_activity_days(
    people: dict[str, int], session_factory: Sessions
) -> None:
    await _active_days(session_factory, people["alice"], TODAY, TODAY - timedelta(days=1))
    await _active_days(session_factory, people["bob"], TODAY)
    async with session_factory() as session:
        await users.delete_account(session, people["alice"], NOON)
        await session.commit()
        owners = (await session.scalars(select(UserActivityDay.user_id))).all()
    assert owners == [people["bob"]]


async def test_activity_days_are_kept_400_days(
    people: dict[str, int], session_factory: Sessions, redis: Redis
) -> None:
    kept = TODAY - timedelta(days=400)
    await _active_days(session_factory, people["alice"], kept, kept - timedelta(days=1), TODAY)
    job = ActivityRetention(redis, session_factory)
    assert await job.tick(NOON) == 1
    assert await job.tick(NOON) is None  # once a day
    async with session_factory() as session:
        days = sorted((await session.scalars(select(UserActivityDay.day))).all())
    assert days == [kept, TODAY]


# --- The dashboard -------------------------------------------------------------------


async def test_only_admins_see_analytics(panel: Panel, people: dict[str, int]) -> None:
    assert _get(panel, MODERATOR, period="7d").status_code == 403
    assert _get(panel, ALICE, period="7d").status_code == 403
    assert _get(panel, period="2w").status_code == 422
    assert _get(panel).status_code == 422  # a period is required


async def test_growth_figures(
    panel: Panel,
    people: dict[str, int],
    session_factory: Sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    today = now.astimezone(growth.ZONE).date()
    alice, bob, carol = people["alice"], people["bob"], people["carol"]
    async with session_factory() as session:
        # Sign-ups: everyone today, except Alice (3 days ago) and Bob (40 days ago).
        await session.execute(
            update(User).where(User.id == alice).values(created_at=now - timedelta(days=3))
        )
        await session.execute(
            update(User).where(User.id == bob).values(created_at=now - timedelta(days=40))
        )
        await session.execute(update(Profile).values(onboarding_completed_at=now))
        await session.execute(update(User).where(User.id == carol).values(status="suspended"))
        await session.execute(
            update(User).where(User.id == bob).values(vip_until=now + timedelta(days=10))
        )
        # A VIP purchase today, and a refunded one that isn't counted.
        for charge, status in (("c1", "paid"), ("c2", "refunded")):
            invoice = StarsInvoice(user_id=bob, product="vip", stars=780, coins=0, days=180)
            session.add(invoice)
            await session.flush()
            session.add(
                StarsPayment(
                    telegram_charge_id=charge,
                    user_id=bob,
                    invoice_id=invoice.id,
                    stars=780,
                    status=status,
                    refunded_at=now if status == "refunded" else None,
                    refund_reason="admin" if status == "refunded" else None,
                    refund_note="test refund" if status == "refunded" else None,
                    vip_until=now + timedelta(days=180),
                )
            )
        await session.commit()
    # Activity: Alice today and 10 days ago; Bob 3 days ago.
    await _active_days(session_factory, alice, today, today - timedelta(days=10))
    await _active_days(session_factory, bob, today - timedelta(days=3))

    response = _get(panel, period="7d")

    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["period"], data["bucket_days"], len(data["buckets"])) == ("7d", 1, 7)
    by_day = {bucket["start"]: bucket for bucket in data["buckets"]}
    last = by_day[today.isoformat()]
    assert last["signups"] == 3  # Carol, the moderator, and the admin
    assert by_day[(today - timedelta(days=3)).isoformat()]["signups"] == 1  # Alice
    assert last["vip_purchases"] == 1
    assert (last["active"], last["active_7d"], last["active_30d"]) == (1, 2, 2)
    totals = data["totals"]
    assert totals["by_status"] == {"active": 4, "suspended": 1}
    assert (
        totals["vip_now"],
        totals["active_today"],
        totals["active_7d"],
        totals["active_30d"],
    ) == (
        1,
        1,
        2,
        2,
    )
    assert totals["onboarded"] == 5
    # Totals only: no one's identity is returned.
    for name in ("Night Owl", "Moon Child", str(ALICE), str(BOB)):
        assert name not in response.text


async def test_a_year_is_shown_by_week(panel: Panel, people: dict[str, int]) -> None:
    data = _get(panel, period="12m").json()
    assert (data["bucket_days"], len(data["buckets"])) == (7, 52)
    starts = [date.fromisoformat(bucket["start"]) for bucket in data["buckets"]]
    assert all((b - a).days == 7 for a, b in pairwise(starts))


async def test_viewing_analytics_is_audited(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    assert _get(panel, period="30d").status_code == 200
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AdminAuditEntry.actor_user_id, AdminAuditEntry.new_value).where(
                    AdminAuditEntry.action == "analytics_viewed"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [(people["admin"], "growth 30d")]


@pytest.mark.parametrize("period", list(Period))
def test_every_period_has_a_span(period: Period) -> None:
    days, size = growth.SPANS[period]
    assert days % size == 0
