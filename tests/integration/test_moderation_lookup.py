"""Looking up accounts in the moderation Mini App, and admins opening stored chats with a
reason (D-071)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.methods import SendPhoto
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin.models import AdminAuditEntry
from backend.modules.chat.models import TelegramDeletion
from backend.modules.direct_messages.models import DirectMessage
from backend.modules.moderation import actions
from backend.modules.moderation.models import ModerationNotice
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from tests.support.moderation import (
    ADMIN,
    BOB,
    MODERATOR,
    NOW,
    Panel,
    make_conversation,
    make_message,
    make_report,
)
from tests.support.webapp import authorization

Sessions = async_sessionmaker[AsyncSession]
REASON = "Appeal from support on 21 Nov: says the ban was a mistake"


async def _public_id(sessions: Sessions, user_id: int) -> str:
    async with sessions() as session:
        public_id = await session.scalar(
            select(Profile.public_id).where(Profile.user_id == user_id)
        )
        assert public_id is not None
        return public_id


async def _audit(sessions: Sessions) -> list[tuple[str, int | None, str | None, str | None]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                AdminAuditEntry.action,
                AdminAuditEntry.target_user_id,
                AdminAuditEntry.new_value,
                AdminAuditEntry.reason,
            )
            .where(
                AdminAuditEntry.action.in_(
                    ("account_looked_up", "chats_listed", "chat_opened", "media_viewed")
                )
            )
            .order_by(AdminAuditEntry.id)
        )
        return [tuple(row) for row in rows.all()]  # type: ignore[misc]


async def _bobs_threads(sessions: Sessions, people: dict[str, int]) -> dict[str, int]:
    """Bob chatted with Alice (a photo and text), and sent Carol a direct message."""
    chat = await make_conversation(sessions, people["alice"], people["bob"])
    photo = await make_message(
        sessions, chat, people["bob"], "photo", file_id="AgAC-chat", sender_message_id=1
    )
    await make_message(
        sessions,
        chat,
        people["alice"],
        "text",
        text="hello",
        sender_message_id=2,
        created_at=NOW + timedelta(minutes=1),
    )
    async with sessions() as session:
        direct = DirectMessage(
            sender_user_id=people["bob"],
            recipient_user_id=people["carol"],
            idempotency_key="dm-lookup",
            kind="photo",
            file_id="AgAC-direct",
            price=0,
            created_at=NOW + timedelta(minutes=5),
        )
        session.add(direct)
        await session.commit()
        return {"chat": chat, "photo": photo, "direct": direct.id}


# --- Finding accounts ---------------------------------------------------------------


async def test_find_by_realchat_id(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    public_id = await _public_id(session_factory, people["bob"])
    # As typed, and as the bot shows it on cards (/userid_<id>).
    for query in (
        public_id,
        public_id.upper(),
        f"  {public_id} ",
        f"/userid_{public_id}",
        f"userid_{public_id}",
        f"/userid {public_id}",
        f"/USERID_{public_id.upper()}",
    ):
        response = panel.client.get(
            "/api/moderation/accounts",
            params={"query": query},
            headers=authorization(MODERATOR, datetime.now(UTC)),
        )
        assert response.status_code == 200, query
        assert response.json() == [
            {
                "id": people["bob"],
                "public_id": public_id,
                "nickname": "Moon Child",
                "status": "active",
            }
        ]
    # Telegram ids and usernames aren't searched (owner decision).
    for query in ("zzzzzzz", str(BOB), "@Laksh69", "Laksh69"):
        nothing = panel.client.get(
            "/api/moderation/accounts",
            params={"query": query},
            headers=authorization(MODERATOR, datetime.now(UTC)),
        )
        assert (nothing.status_code, nothing.json()) == (200, []), query
    audit = await _audit(session_factory)
    assert audit[0] == ("account_looked_up", people["bob"], "found", None)
    assert [row[2] for row in audit[-4:]] == [
        "not found",
        "not a RealChat ID",
        "not a RealChat ID",
        "not a RealChat ID",
    ]


async def test_banned_accounts_are_found_but_deleted_ones_are_not(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    public_id = await _public_id(session_factory, people["bob"])
    async with session_factory() as session:
        await actions.act(
            session, people["admin"], people["bob"], actions.Kind.BAN, NOW, rule="spam"
        )
        await session.commit()
    found = panel.get(f"/api/moderation/accounts?query={public_id}").json()
    assert [(m["id"], m["status"]) for m in found] == [(people["bob"], "banned")]

    async with session_factory() as session:
        await users.delete_account(session, people["bob"], NOW)
        await session.commit()
    # Deletion erases the profile and its RealChat ID.
    assert panel.get(f"/api/moderation/accounts?query={public_id}").json() == []


# --- The account --------------------------------------------------------------------


async def test_the_account_shows_its_record_and_reports(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await actions.act(
            session,
            people["admin"],
            people["bob"],
            actions.Kind.SUSPEND,
            NOW,
            rule="threats",
            days=3,
        )
        await session.commit()
    received = await make_report(session_factory, people["alice"], people["bob"], None)
    made = await make_report(session_factory, people["bob"], people["carol"], None, reason="spam")

    response = panel.get(f"/api/moderation/accounts/{people['bob']}")

    assert response.status_code == 200
    data = response.json()
    assert data["person"]["nickname"] == "Moon Child"
    assert data["person"]["status"] == "suspended"
    assert data["suspended_until"] is not None
    assert [(a["kind"], a["rule"], a["days"]) for a in data["record"]["actions"]] == [
        ("suspend", "threats", 3)
    ]
    assert [r["id"] for r in data["reports_received"]] == [received]
    assert [(r["id"], r["reason"]) for r in data["reports_made"]] == [(made, "spam")]
    assert str(BOB) not in response.text
    assert panel.get("/api/moderation/accounts/999999").status_code == 404


# --- Actions from a lookup ----------------------------------------------------------


async def test_actions_from_a_lookup_follow_the_usual_rules(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    bob = people["bob"]
    path = f"/api/moderation/accounts/{bob}/action"
    assert panel.post(path, {"kind": "suspend", "rule": "spam", "days": 1}).status_code == 200
    # A moderator can lift a suspension.
    assert panel.post(path, {"kind": "lift"}).status_code == 200
    assert panel.post(path, {"kind": "ban", "rule": "threats"}).status_code == 200
    # Only an admin can lift a ban.
    refused = panel.post(path, {"kind": "lift"})
    assert (refused.status_code, refused.json()) == (409, {"error": "admin_only"})
    assert panel.post(path, {"kind": "lift"}, ADMIN).status_code == 200
    async with session_factory() as session:
        user = await session.get(User, bob)
        assert user is not None and user.status == "active"
        notices = (
            await session.scalars(
                select(ModerationNotice.kind).where(ModerationNotice.recipient_user_id == bob)
            )
        ).all()
        assert notices == ["action"] * 4  # suspend, lift, ban, lift
    # Closing needs a report, so it isn't offered here; staff are protected.
    assert panel.post(path, {"kind": "close"}).status_code == 422
    staff = panel.post(
        f"/api/moderation/accounts/{people['admin']}/action", {"kind": "warn", "rule": "spam"}
    )
    assert (staff.status_code, staff.json()) == (409, {"error": "protected"})
    assert panel.post("/api/moderation/accounts/999999/action", {"kind": "lift"}).status_code == 404


# --- Admins opening stored chats ----------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("threads", {"reason": REASON}),
        ("threads/open", {"reason": REASON, "kind": "chat", "ref": 1}),
        ("media", {"reason": REASON, "source": "chat", "item_id": 1}),
    ],
)
async def test_moderators_cannot_open_stored_chats(
    panel: Panel, people: dict[str, int], session_factory: Sessions, path: str, body: Any
) -> None:
    await _bobs_threads(session_factory, people)
    response = panel.post(f"/api/moderation/accounts/{people['bob']}/{path}", body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert panel.telegram.requests == []
    assert await _audit(session_factory) == []


@pytest.mark.parametrize("reason", [None, "", "   ", "abcd", "x" * 301])
async def test_a_reason_is_required(
    panel: Panel, people: dict[str, int], session_factory: Sessions, reason: str | None
) -> None:
    body = {} if reason is None else {"reason": reason}
    response = panel.post(f"/api/moderation/accounts/{people['bob']}/threads", body, ADMIN)
    assert response.status_code == 422
    assert await _audit(session_factory) == []


async def test_an_admin_lists_and_opens_threads_with_a_reason(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    bob = people["bob"]
    ids = await _bobs_threads(session_factory, people)

    listed = panel.post(
        f"/api/moderation/accounts/{bob}/threads", {"reason": f"  {REASON}  "}, ADMIN
    )

    assert listed.status_code == 200
    threads = listed.json()
    assert [(t["kind"], t["ref"], t["partner_nickname"], t["messages"]) for t in threads] == [
        ("direct", people["carol"], "Sun Ray", 1),
        ("chat", ids["chat"], "Night Owl", 2),
    ]
    assert "AgAC" not in listed.text and "hello" not in listed.text  # no content

    opened = panel.post(
        f"/api/moderation/accounts/{bob}/threads/open",
        {"reason": REASON, "kind": "chat", "ref": ids["chat"]},
        ADMIN,
    )
    assert opened.status_code == 200
    assert [(m["sender"], m["kind"], m["viewable"]) for m in opened.json()] == [
        ("account", "photo", True),
        ("partner", "text", False),
    ]
    direct = panel.post(
        f"/api/moderation/accounts/{bob}/threads/open",
        {"reason": REASON, "kind": "direct", "ref": people["carol"]},
        ADMIN,
    )
    assert [m["sender"] for m in direct.json()] == ["account"]

    assert await _audit(session_factory) == [
        ("chats_listed", bob, f"account {bob}", REASON),
        ("chat_opened", bob, f"account {bob} chat {ids['chat']}", REASON),
        ("chat_opened", bob, f"account {bob} direct {people['carol']}", REASON),
    ]


async def test_threads_the_account_took_no_part_in_are_refused(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    ids = await _bobs_threads(session_factory, people)
    alice = people["alice"]
    # From Carol's account: a chat she wasn't in, a thread with no messages, herself.
    for body in (
        {"reason": REASON, "kind": "chat", "ref": ids["chat"]},  # Carol wasn't in it
        {"reason": REASON, "kind": "direct", "ref": alice},  # no messages between them
        {"reason": REASON, "kind": "direct", "ref": people["carol"]},  # herself
    ):
        response = panel.post(
            f"/api/moderation/accounts/{people['carol']}/threads/open", body, ADMIN
        )
        assert response.status_code == 404, body
    assert await _audit(session_factory) == []


async def test_an_admin_views_media_only_from_the_accounts_threads(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    ids = await _bobs_threads(session_factory, people)
    bob, carol = people["bob"], people["carol"]

    sent = panel.post(
        f"/api/moderation/accounts/{bob}/media",
        {"reason": REASON, "source": "chat", "item_id": ids["photo"]},
        ADMIN,
    )
    assert (sent.status_code, sent.json()) == (200, {"deleted_after_seconds": 600})
    copy = panel.telegram.requests[-1]
    assert isinstance(copy, SendPhoto)
    assert (copy.chat_id, copy.protect_content, copy.photo) == (ADMIN, True, "AgAC-chat")
    # Carol received the direct message, so it's in her threads too.
    assert (
        panel.post(
            f"/api/moderation/accounts/{carol}/media",
            {"reason": REASON, "source": "direct", "item_id": ids["direct"]},
            ADMIN,
        ).status_code
        == 200
    )
    # But Bob and Alice's chat photo isn't in Carol's threads.
    assert (
        panel.post(
            f"/api/moderation/accounts/{carol}/media",
            {"reason": REASON, "source": "chat", "item_id": ids["photo"]},
            ADMIN,
        ).status_code
        == 404
    )
    assert (
        panel.post(
            f"/api/moderation/accounts/{bob}/media",
            {"reason": REASON, "source": "photo", "item_id": 1},
            ADMIN,
        ).status_code
        == 422
    )

    async with session_factory() as session:
        assert len((await session.scalars(select(TelegramDeletion))).all()) == 2
    media = [row for row in await _audit(session_factory) if row[0] == "media_viewed"]
    assert media == [
        ("media_viewed", bob, f"account {bob} chat {ids['photo']}", REASON),
        ("media_viewed", carol, f"account {carol} direct {ids['direct']}", REASON),
    ]


async def test_an_inactive_admin_is_refused(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.id == people["admin"]).values(status="suspended")
        )
        await session.commit()
    response = panel.post(
        f"/api/moderation/accounts/{people['bob']}/threads", {"reason": REASON}, ADMIN
    )
    assert response.status_code == 403


async def test_a_deleted_account_cant_be_reached_by_its_internal_id(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    # Lookup finds only what a RealChat ID search would: the account endpoints refuse
    # an account deleted since, even when its internal id is known.
    bob = people["bob"]
    await _bobs_threads(session_factory, people)
    async with session_factory() as session:
        await actions.act(session, people["admin"], bob, actions.Kind.BAN, NOW, rule="spam")
        await users.delete_account(session, bob, NOW)
        await session.commit()
    base = f"/api/moderation/accounts/{bob}"
    assert panel.get(base).status_code == 404
    assert panel.post(f"{base}/action", {"kind": "lift"}, ADMIN).status_code == 404
    assert panel.post(f"{base}/threads", {"reason": REASON}, ADMIN).status_code == 404
    opened = panel.post(
        f"{base}/threads/open", {"reason": REASON, "kind": "direct", "ref": people["carol"]}, ADMIN
    )
    assert opened.status_code == 404
    media = panel.post(f"{base}/media", {"reason": REASON, "source": "direct", "item_id": 1}, ADMIN)
    assert media.status_code == 404
    async with session_factory() as session:
        user = await session.get(User, bob)
        assert user is not None and user.status == "banned"  # the lift was refused


async def test_viewing_an_account_is_audited(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    assert panel.get(f"/api/moderation/accounts/{people['bob']}").status_code == 200
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AdminAuditEntry.action, AdminAuditEntry.target_user_id).where(
                    AdminAuditEntry.action == "account_viewed"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [("account_viewed", people["bob"])]


async def test_an_unfinished_profile_isnt_found(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    async with session_factory() as session:
        user = await users.get_or_create_user(session, 7300)  # started, never onboarded
        await session.commit()
        public_id = await session.scalar(
            select(Profile.public_id).where(Profile.user_id == user.id)
        )
    assert panel.get(f"/api/moderation/accounts?query={public_id}").json() == []
    assert panel.get(f"/api/moderation/accounts/{user.id}").status_code == 404
