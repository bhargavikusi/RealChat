"""The user directory in the admin panel: search, filters, sorting, paging (D-072)."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin import directory
from backend.modules.admin.models import AdminAuditEntry
from backend.modules.chat.models import Conversation, ConversationParticipant
from backend.modules.coins import service as coins
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from tests.support.moderation import ADMIN, ALICE, BOB, CAROL, MODERATOR, Panel, make_report

Sessions = async_sessionmaker[AsyncSession]
NOW = datetime.now(UTC)


async def _chats(sessions: Sessions, a: int, b: int | None, count: int, days_ago: int = 1) -> None:
    """`count` ended chats of a (with b, if given), started `days_ago` days ago."""
    started = NOW - timedelta(days=days_ago)
    async with sessions() as session:
        for _ in range(count):
            conversation = Conversation(
                started_at=started, ended_at=started, end_reason="user_ended"
            )
            session.add(conversation)
            await session.flush()
            session.add(
                ConversationParticipant(conversation_id=conversation.id, user_id=a, left_at=started)
            )
            if b is not None:
                session.add(
                    ConversationParticipant(
                        conversation_id=conversation.id, user_id=b, left_at=started
                    )
                )
        await session.commit()


@pytest.fixture
async def world(people: dict[str, int], session_factory: Sessions) -> dict[str, int]:
    """Alice: joined long ago, inactive, a man from the US aged 40, 500 coins.
    Bob: VIP, very active (5 recent chats), reported twice, 20 coins.
    Carol: banned, joined recently."""
    alice, bob, carol = people["alice"], people["bob"], people["carol"]
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == alice)
            .values(created_at=NOW - timedelta(days=200), last_active_at=NOW - timedelta(days=60))
        )
        await session.execute(
            update(Profile)
            .where(Profile.user_id == alice)
            .values(gender="male", country_code="US", age_at_entry=40, age_entered_on=NOW.date())
        )
        await session.execute(
            update(User)
            .where(User.id == bob)
            .values(
                vip_until=NOW + timedelta(days=30),
                created_at=NOW - timedelta(days=50),
                last_active_at=NOW,
            )
        )
        await session.execute(
            update(User)
            .where(User.id == carol)
            .values(status="banned", created_at=NOW - timedelta(days=2))
        )
        await coins.credit(session, alice, 500, "admin_grant", "g-alice", note="test")
        await coins.credit(session, bob, 20, "admin_grant", "g-bob", note="test")
        await session.commit()
    await _chats(session_factory, bob, None, 5)  # Bob's alone, so only he counts them
    await _chats(session_factory, alice, bob, 1, days_ago=45)  # too old to count
    await make_report(session_factory, alice, bob, None)
    await make_report(session_factory, carol, bob, None, reason="spam")
    return people


def _list(panel: Panel, as_user: int = ADMIN, **params: Any) -> Any:
    from tests.support.webapp import authorization

    return panel.client.get(
        "/api/moderation/users", params=params, headers=authorization(as_user, datetime.now(UTC))
    )


def _names(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["nickname"] for row in response.json()["rows"]]


async def test_only_admins_can_list_users(panel: Panel, world: dict[str, int]) -> None:
    assert _list(panel, MODERATOR).status_code == 403
    assert _list(panel, ALICE).status_code == 403
    assert panel.client.get("/api/moderation/users").status_code == 401


async def test_every_active_onboarded_account_is_listed_without_telegram_ids(
    panel: Panel, world: dict[str, int], session_factory: Sessions
) -> None:
    # One account that never finished onboarding, and one deleted: never listed.
    async with session_factory() as session:
        await users.get_or_create_user(session, 9001)
        await users.delete_account(session, world["alice"], NOW)
        await session.commit()
    response = _list(panel)
    assert sorted(_names(response)) == sorted(["Moon Child", "Sun Ray", "Mod Max", "Admin Ann"])
    assert response.json()["total"] == 4
    for telegram_id in (ALICE, BOB, CAROL, MODERATOR, ADMIN, 9001):
        assert f":{telegram_id}," not in response.text and f":{telegram_id}}}" not in response.text


async def test_rows_carry_the_figures(panel: Panel, world: dict[str, int]) -> None:
    rows = {row["nickname"]: row for row in _list(panel).json()["rows"]}
    bob = rows["Moon Child"]
    assert (bob["chats_30d"], bob["coins"], bob["reports_received"]) == (5, 20, 2)
    assert bob["vip_until"] is not None
    alice = rows["Night Owl"]
    assert (alice["chats_30d"], alice["coins"], alice["age"], alice["vip_until"]) == (
        0,
        500,
        40,
        None,
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"vip": "true"}, ["Moon Child"]),
        ({"status": "banned"}, ["Sun Ray"]),
        ({"role": "moderator"}, ["Mod Max"]),
        ({"min_chats": 3}, ["Moon Child"]),
        ({"min_reports": 2}, ["Moon Child"]),
        ({"min_coins": 100}, ["Night Owl"]),
        ({"max_coins": 0, "role": "admin"}, ["Admin Ann"]),
        ({"country": "US"}, ["Night Owl"]),
        ({"gender": "male"}, ["Night Owl"]),
        ({"min_age": 30}, ["Night Owl"]),
        ({"query": "moon"}, ["Moon Child"]),
        ({"query": "%"}, []),  # a LIKE wildcard is matched literally
    ],
)
async def test_filters(
    panel: Panel, world: dict[str, int], params: dict[str, Any], expected: list[str]
) -> None:
    assert sorted(_names(_list(panel, **params))) == sorted(expected)


async def test_activity_and_date_filters(panel: Panel, world: dict[str, int]) -> None:
    active = _names(_list(panel, active_within_days=30))
    assert "Night Owl" not in active and "Moon Child" in active
    recent = _names(_list(panel, joined_from=(NOW - timedelta(days=10)).date().isoformat()))
    assert "Sun Ray" in recent and "Night Owl" not in recent
    early = _names(_list(panel, joined_to=(NOW - timedelta(days=100)).date().isoformat()))
    assert early == ["Night Owl"]


async def test_search_by_realchat_id(
    panel: Panel, world: dict[str, int], session_factory: Sessions
) -> None:
    async with session_factory() as session:
        public_id = await session.scalar(
            select(Profile.public_id).where(Profile.user_id == world["carol"])
        )
    assert _names(_list(panel, query=str(public_id).upper())) == ["Sun Ray"]


@pytest.mark.parametrize(
    ("sort", "first"),
    [("chats", "Moon Child"), ("coins", "Night Owl"), ("reports", "Moon Child"), ("newest", None)],
)
async def test_sorting(panel: Panel, world: dict[str, int], sort: str, first: str | None) -> None:
    names = _names(_list(panel, sort=sort))
    if first is not None:
        assert names[0] == first
    ascending = _names(_list(panel, sort=sort, descending="false"))
    assert ascending[-1] == names[0]


async def test_paging(panel: Panel, world: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directory, "PAGE_SIZE", 2)
    first, second, third = (_list(panel, page=page).json() for page in (0, 1, 2))
    assert first["total"] == 5 and first["page_size"] == 2
    names = [row["nickname"] for page in (first, second, third) for row in page["rows"]]
    assert len(names) == 5 and len(set(names)) == 5


@pytest.mark.parametrize(
    "params",
    [
        {"status": "deleted"},
        {"role": "owner"},
        {"country": "usa"},
        {"gender": "x"},
        {"min_chats": -1},
        {"page": -1},
        {"sort": "telegram_id"},
        {"query": "x" * 65},
        {"unknown": 1},
    ],
)
async def test_invalid_filters_are_refused(
    panel: Panel, world: dict[str, int], params: dict[str, Any]
) -> None:
    assert _list(panel, **params).status_code == 422


async def test_listing_is_audited_with_the_filters(
    panel: Panel, world: dict[str, int], session_factory: Sessions
) -> None:
    assert _list(panel, vip="true", sort="chats").status_code == 200
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    AdminAuditEntry.actor_user_id, AdminAuditEntry.new_value, AdminAuditEntry.reason
                ).where(AdminAuditEntry.action == "users_listed")
            )
        ).all()
    assert len(rows) == 1
    actor, value, reason = rows[0]
    assert (actor, value) == (world["admin"], "page 0")
    assert reason is not None and "vip=True" in reason and "sort=chats" in reason


def test_filters_describe_only_what_is_set() -> None:
    assert directory.Filters().describe() == "sort=newest, descending=True"
    assert (
        date(2026, 1, 1).isoformat() in directory.Filters(joined_from=date(2026, 1, 1)).describe()
    )
