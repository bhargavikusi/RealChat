"""Moderation analytics for admins (D-072)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin.models import AdminAuditEntry
from backend.modules.analytics.periods import ZONE
from backend.modules.moderation.models import ModerationAction
from backend.modules.users import service as users
from tests.support.moderation import ADMIN, ALICE, MODERATOR, Panel, make_person, make_report
from tests.support.webapp import authorization

Sessions = async_sessionmaker[AsyncSession]
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _get(panel: Panel, as_user: int = ADMIN, **params: Any) -> Any:
    return panel.client.get(
        "/api/moderation/analytics/moderation",
        params=params,
        headers=authorization(as_user, datetime.now(UTC)),
    )


async def _action(
    sessions: Sessions, actor: int, target: int, kind: str, at: datetime, **fields: Any
) -> None:
    async with sessions() as session:
        session.add(
            ModerationAction(
                actor_user_id=actor, target_user_id=target, kind=kind, created_at=at, **fields
            )
        )
        await session.commit()


def _closed(at: datetime, outcome: str = "action_taken") -> dict[str, Any]:
    return {"status": "closed", "closed_at": at, "outcome": outcome}


async def test_only_admins_see_moderation_analytics(panel: Panel, people: dict[str, int]) -> None:
    assert _get(panel, MODERATOR, period="7d").status_code == 403
    assert _get(panel, ALICE, period="7d").status_code == 403
    assert _get(panel, period="1d").status_code == 422
    assert _get(panel).status_code == 422


async def test_moderation_figures(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    today = now.astimezone(ZONE).date()
    alice, bob, carol = people["alice"], people["bob"], people["carol"]
    moderator, admin = people["moderator"], people["admin"]

    # Made in the period: two about Bob, one about Alice (urgent), one about Carol.
    await make_report(session_factory, alice, bob, None, reason="spam", created_at=now)
    await make_report(
        session_factory,
        carol,
        bob,
        None,
        reason="harassment",
        created_at=now - 2 * DAY,
        **_closed(now - 2 * DAY + 2 * HOUR),
    )
    await make_report(
        session_factory, bob, alice, None, reason="other", urgent=True, created_at=now - DAY
    )
    await make_report(session_factory, bob, carol, None, reason="threats", created_at=now)
    # Made before it: the oldest open report, one closed in the period, and one closed
    # before it.
    oldest = now - 10 * DAY
    await make_report(session_factory, admin, bob, None, reason="sexual", created_at=oldest)
    await make_report(
        session_factory,
        alice,
        carol,
        None,
        reason="spam",
        created_at=now - 20 * DAY,
        **_closed(now - 3 * DAY, "no_action"),
    )
    await make_report(
        session_factory,
        moderator,
        carol,
        None,
        reason="spam",
        created_at=now - 12 * DAY,
        **_closed(now - 9 * DAY),
    )

    # Actions: three by the moderator and one by the admin in the period, one before it.
    await _action(session_factory, moderator, bob, "warn", now, rule="spam")
    await _action(
        session_factory, moderator, bob, "suspend", now - DAY, rule="spam", days=1, ends_at=now
    )
    await _action(session_factory, moderator, alice, "lift", now - 2 * DAY)
    await _action(session_factory, admin, carol, "ban", now - 3 * DAY, rule="threats")
    # Carol was staff once: her action still counts after she deletes her account.
    await _action(session_factory, carol, alice, "warn", now - 4 * DAY, rule="spam")
    await _action(session_factory, moderator, carol, "warn", now - 9 * DAY, rule="spam")

    # Carol deletes her account: still counted, never named.
    async with session_factory() as session:
        await users.delete_account(session, carol, now)
        await session.commit()

    response = _get(panel, period="7d")

    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["period"], data["bucket_days"], len(data["buckets"])) == ("7d", 1, 7)
    by_day = {bucket["start"]: bucket["reports"] for bucket in data["buckets"]}
    zero = {
        reason: 0 for reason in ("spam", "harassment", "sexual", "under_18", "threats", "other")
    }
    assert by_day[today.isoformat()] == {**zero, "spam": 1, "threats": 1}
    assert by_day[(today - timedelta(days=1)).isoformat()] == {**zero, "other": 1}
    assert by_day[(today - timedelta(days=2)).isoformat()] == {**zero, "harassment": 1}
    assert sum(sum(reasons.values()) for reasons in by_day.values()) == 4

    backlog = data["backlog"]
    assert (backlog["open"], backlog["urgent"]) == (4, 1)
    assert datetime.fromisoformat(backlog["oldest_open_at"]) == oldest

    # 2 hours and 17 days.
    assert data["handling"] == {
        "closed": 2,
        "action_taken": 1,
        "no_action": 1,
        "median_seconds": (2 * 3600 + 17 * 86400) / 2,
        "longest_seconds": 17 * 86400,
    }

    actions = {kind: 0 for kind in ("close", "warn", "suspend", "ban", "lift")}
    assert data["moderators"] == [
        {
            "nickname": "Mod Max",
            "public_id": await _public_id(session_factory, moderator),
            "actions": {**actions, "warn": 1, "suspend": 1, "lift": 1},
            "total": 3,
        },
        {
            "nickname": "Admin Ann",
            "public_id": await _public_id(session_factory, admin),
            "actions": {**actions, "ban": 1},
            "total": 1,
        },
        {"nickname": None, "public_id": None, "actions": {**actions, "warn": 1}, "total": 1},
    ]

    # Bob has the most; Carol and Alice one each, Carol's the more recent.
    assert [
        (row["id"], row["nickname"], row["reports"], row["open"], row["findable"])
        for row in data["most_reported"]
    ] == [
        (bob, "Moon Child", 2, 1, True),
        (carol, None, 1, 1, False),
        (alice, "Night Owl", 1, 1, True),
    ]
    assert data["most_reported"][1]["public_id"] is None
    assert data["most_reported"][1]["status"] == "deleted"


async def _public_id(sessions: Sessions, user_id: int) -> str:
    async with sessions() as session:
        profile = await users.get_profile(session, user_id)
        assert profile is not None
        return profile.public_id


async def test_handling_is_empty_without_closed_reports(
    panel: Panel, people: dict[str, int]
) -> None:
    data = _get(panel, period="30d").json()
    assert data["handling"]["median_seconds"] is None
    assert data["handling"]["longest_seconds"] is None
    assert data["backlog"] == {"open": 0, "urgent": 0, "oldest_open_at": None}
    assert (data["moderators"], data["most_reported"]) == ([], [])


async def test_only_the_ten_most_reported_are_listed(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    reporters = [await make_person(session_factory, 8000 + n, f"Reporter {n}") for n in range(12)]
    # Bob gets 12 reports; each of the 12 reporters gets one.
    for reporter in reporters:
        await make_report(session_factory, reporter, people["bob"], None, created_at=now)
        await make_report(session_factory, people["alice"], reporter, None, created_at=now)
    data = _get(panel, period="7d").json()
    assert len(data["most_reported"]) == 10
    assert data["most_reported"][0]["reports"] == 12


async def test_viewing_moderation_analytics_is_audited(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    assert _get(panel, period="90d").status_code == 200
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AdminAuditEntry.actor_user_id, AdminAuditEntry.new_value).where(
                    AdminAuditEntry.action == "analytics_viewed"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [(people["admin"], "moderation 90d")]


async def test_older_reports_stay_out_of_the_first_week(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    # A year is 52 weeks ending today: this falls three days before its first day.
    before = datetime.now(UTC) - 366 * DAY
    await make_report(session_factory, people["alice"], people["bob"], None, created_at=before)
    buckets = _get(panel, period="12m").json()["buckets"]
    assert len(buckets) == 52
    assert all(sum(bucket["reports"].values()) == 0 for bucket in buckets)


async def test_nothing_after_today_is_counted(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    later = datetime.now(UTC) + 2 * DAY
    alice, bob = people["alice"], people["bob"]
    await make_report(session_factory, alice, bob, None, created_at=later - HOUR, **_closed(later))
    await _action(session_factory, people["moderator"], bob, "warn", later, rule="spam")
    data = _get(panel, period="7d").json()
    assert data["handling"]["closed"] == 0
    assert (data["moderators"], data["most_reported"]) == ([], [])
