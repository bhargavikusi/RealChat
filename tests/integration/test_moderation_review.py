"""Reviewing reports in the moderation Mini App: the queue, the review, media, and
actions (D-071)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import SendPhoto, SendVideo
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api import moderation
from apps.api.main import create_app
from backend.config.settings import BotSettings
from backend.modules.admin.models import AdminAuditEntry
from backend.modules.chat.models import (
    ChatMessage,
    Conversation,
    ConversationParticipant,
    PrivateSession,
    TelegramDeletion,
)
from backend.modules.chat_requests.models import ChatRequest
from backend.modules.coins import service as coins
from backend.modules.direct_messages.models import DirectMessage
from backend.modules.moderation import actions, review
from backend.modules.moderation.models import ModerationNotice, Report
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from tests.support.telegram import RecordingSession, make_bot
from tests.support.webapp import authorization

Sessions = async_sessionmaker[AsyncSession]
ALICE, BOB, CAROL, MODERATOR, ADMIN = 6001, 6002, 6003, 7101, 7102
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class Panel:
    """The API with a recording bot, and helpers to call it as a signed-in person."""

    def __init__(self, client: TestClient, telegram: RecordingSession) -> None:
        self.client = client
        self.telegram = telegram

    def get(self, path: str, as_user: int = MODERATOR) -> Any:
        return self.client.get(path, headers=authorization(as_user, datetime.now(UTC)))

    def post(self, path: str, body: dict[str, Any], as_user: int = MODERATOR) -> Any:
        return self.client.post(path, json=body, headers=authorization(as_user, datetime.now(UTC)))


@pytest.fixture
def panel(
    bot_settings: BotSettings,
    session_factory: Sessions,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Panel]:
    monkeypatch.setattr(moderation, "DIST_DIR", tmp_path)
    with TestClient(create_app(bot_settings)) as client:
        bot, telegram = make_bot()
        client.app.state.bot = bot  # type: ignore[attr-defined]
        yield Panel(client, telegram)


async def _person(
    sessions: Sessions,
    telegram_id: int,
    nickname: str,
    role: str = "user",
    photo: str | None = None,
) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        user.role = role
        await session.execute(
            update(Profile)
            .where(Profile.user_id == user.id)
            .values(
                nickname=nickname,
                age_at_entry=25,
                age_entered_on=date(2026, 1, 1),
                gender="female",
                country_code="IN",
                photo_file_id=photo,
            )
        )
        await session.commit()
        return user.id


@pytest.fixture
async def people(session_factory: Sessions) -> dict[str, int]:
    return {
        "alice": await _person(session_factory, ALICE, "Night Owl"),
        "bob": await _person(session_factory, BOB, "Moon Child", photo="AgAC-bob-photo"),
        "carol": await _person(session_factory, CAROL, "Sun Ray"),
        "moderator": await _person(session_factory, MODERATOR, "Mod Max", "moderator"),
        "admin": await _person(session_factory, ADMIN, "Admin Ann", "admin"),
    }


async def _conversation(sessions: Sessions, a: int, b: int) -> int:
    async with sessions() as session:
        conversation = Conversation(started_at=NOW, ended_at=NOW, end_reason="reported")
        session.add(conversation)
        await session.flush()
        session.add_all(
            [
                ConversationParticipant(conversation_id=conversation.id, user_id=a, left_at=NOW),
                ConversationParticipant(conversation_id=conversation.id, user_id=b, left_at=NOW),
            ]
        )
        await session.commit()
        return conversation.id


async def _message(
    sessions: Sessions, conversation_id: int, sender: int, kind: str, **fields: Any
) -> int:
    async with sessions() as session:
        message = ChatMessage(
            conversation_id=conversation_id,
            sender_user_id=sender,
            sender_message_id=fields.pop("sender_message_id"),
            kind=kind,
            created_at=fields.pop("created_at", NOW),
            **fields,
        )
        session.add(message)
        await session.commit()
        return message.id


async def _report(
    sessions: Sessions, reporter: int, reported: int, conversation_id: int | None, **fields: Any
) -> int:
    async with sessions() as session:
        report = Report(
            reporter_user_id=reporter,
            reported_user_id=reported,
            conversation_id=conversation_id,
            reason=fields.pop("reason", "harassment"),
            urgent=fields.pop("urgent", False),
            created_at=fields.pop("created_at", NOW),
            **fields,
        )
        session.add(report)
        await session.commit()
        return report.id


async def _chat_report(sessions: Sessions, people: dict[str, int]) -> tuple[int, dict[str, int]]:
    """Alice reports Bob's chat, which has text, an edit, a photo, a Private mode
    view-once video, and a dice. Returns the report and the message ids."""
    conversation = await _conversation(sessions, people["alice"], people["bob"])
    async with sessions() as session:
        private = PrivateSession(
            conversation_id=conversation, started_by_user_id=people["bob"], started_at=NOW
        )
        session.add(private)
        await session.commit()
        private_id = private.id
    alice, bob = people["alice"], people["bob"]
    ids = {
        "hello": await _message(
            sessions, conversation, alice, "text", text="hi", sender_message_id=1
        ),
        "rude": await _message(
            sessions,
            conversation,
            bob,
            "text",
            text="rude",
            edited_text="ruder",
            edited_at=NOW,
            sender_message_id=2,
            created_at=NOW + timedelta(seconds=1),
        ),
        "photo": await _message(
            sessions,
            conversation,
            bob,
            "photo",
            text="look",
            file_id="AgAC-photo",
            sender_message_id=3,
            created_at=NOW + timedelta(seconds=2),
        ),
        "video": await _message(
            sessions,
            conversation,
            bob,
            "video",
            file_id="BAAC-video",
            private_session_id=private_id,
            view_once=True,
            sender_message_id=4,
            created_at=NOW + timedelta(seconds=3),
        ),
        "dice": await _message(
            sessions,
            conversation,
            bob,
            "dice",
            sender_message_id=5,
            created_at=NOW + timedelta(seconds=4),
        ),
    }
    report = await _report(sessions, alice, bob, conversation, details="He was rude")
    return report, ids


async def _conversation_of(sessions: Sessions, report_id: int) -> int:
    async with sessions() as session:
        report = await session.get(Report, report_id)
        assert report is not None and report.conversation_id is not None
        return report.conversation_id


async def _audit(sessions: Sessions) -> list[tuple[str, str | None]]:
    async with sessions() as session:
        rows = await session.execute(
            select(AdminAuditEntry.action, AdminAuditEntry.new_value)
            .where(AdminAuditEntry.action.in_(("report_viewed", "media_viewed")))
            .order_by(AdminAuditEntry.id)
        )
        return [(action, value) for action, value in rows.all()]


# --- Access -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/moderation/reports", None),
        ("get", "/api/moderation/reports/1", None),
        ("post", "/api/moderation/reports/1/media", {"source": "photo"}),
        ("post", "/api/moderation/reports/1/action", {"kind": "close"}),
    ],
)
async def test_every_endpoint_needs_an_active_moderator(
    panel: Panel,
    people: dict[str, int],
    session_factory: Sessions,
    method: str,
    path: str,
    body: Any,
) -> None:
    def call(telegram_id: int | None) -> int:
        headers = {} if telegram_id is None else authorization(telegram_id, datetime.now(UTC))
        if method == "get":
            return int(panel.client.get(path, headers=headers).status_code)
        return int(panel.client.post(path, json=body, headers=headers).status_code)

    assert call(None) == 401
    assert call(ALICE) == 403
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.id == people["moderator"]).values(status="banned")
        )
        await session.commit()
    assert call(MODERATOR) == 403


# --- Queue --------------------------------------------------------------------------


async def test_the_queue_lists_open_reports_urgent_first(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    alice, bob, carol = people["alice"], people["bob"], people["carol"]
    older = await _report(session_factory, alice, bob, None, created_at=NOW - timedelta(hours=2))
    urgent = await _report(session_factory, carol, bob, None, reason="under_18", urgent=True)
    newer = await _report(
        session_factory, bob, carol, None, created_at=NOW - timedelta(hours=1), details="spam"
    )
    await _report(
        session_factory, carol, alice, None, status="closed", closed_at=NOW, outcome="no_action"
    )

    response = panel.get("/api/moderation/reports")

    assert response.status_code == 200
    queue = response.json()
    assert [item["id"] for item in queue] == [urgent, older, newer]
    assert queue[0] == {
        "id": urgent,
        "reason": "under_18",
        "urgent": True,
        "kind": "profile",
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "has_details": False,
        "reported_nickname": "Moon Child",
    }
    assert queue[2]["has_details"] is True
    assert "spam" not in response.text  # details aren't in the queue
    assert await _audit(session_factory) == []


# --- Review -------------------------------------------------------------------------


async def test_a_chat_report_shows_the_whole_chat_and_the_record(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    report_id, ids = await _chat_report(session_factory, people)
    # Bob's record: a past warning and another report.
    async with session_factory() as session:
        await actions.act(
            session,
            people["admin"],
            people["bob"],
            actions.Kind.WARN,
            NOW - timedelta(days=3),
            rule="spam",
        )
        await session.commit()
    await _report(session_factory, people["carol"], people["bob"], None)

    response = panel.get(f"/api/moderation/reports/{report_id}")

    assert response.status_code == 200
    data = response.json()
    assert (data["kind"], data["reason"], data["details"], data["status"]) == (
        "chat",
        "harassment",
        "He was rude",
        "open",
    )
    assert data["reported"]["nickname"] == "Moon Child"
    assert data["reported"]["has_photo"] is True
    assert data["reporter"]["nickname"] == "Night Owl"
    assert data["record"]["reports_received"] == 2
    assert data["record"]["reports_open"] == 2
    assert [(a["kind"], a["rule"]) for a in data["record"]["actions"]] == [("warn", "spam")]
    messages = data["messages"]
    assert [m["id"] for m in messages] == list(ids.values())
    by_id = {m["id"]: m for m in messages}
    assert by_id[ids["hello"]]["sender"] == "reporter"
    assert (by_id[ids["rude"]]["text"], by_id[ids["rude"]]["edited_text"]) == ("rude", "ruder")
    assert by_id[ids["photo"]]["viewable"] is True
    assert (by_id[ids["video"]]["private"], by_id[ids["video"]]["view_once"]) == (True, True)
    assert by_id[ids["dice"]]["viewable"] is False
    assert data["requests"] == []
    # Never Telegram ids or file references.
    for secret in (str(ALICE), str(BOB), "AgAC-photo", "BAAC-video", "AgAC-bob-photo"):
        assert secret not in response.text
    assert await _audit(session_factory) == [("report_viewed", f"report {report_id}")]


async def test_a_profile_report_shows_their_direct_contact(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    alice, bob, carol = people["alice"], people["bob"], people["carol"]
    async with session_factory() as session:
        await coins.credit(session, alice, 2, "admin_grant", "grant:test", note="test")
        hold_id = await coins.hold(session, alice, 2, "chat_request", "request:test")
        session.add_all(
            [
                DirectMessage(
                    sender_user_id=bob,
                    recipient_user_id=alice,
                    idempotency_key="dm-1",
                    kind="photo",
                    file_id="AgAC-dm",
                    price=0,
                    created_at=NOW,
                ),
                # Someone else's message is never shown.
                DirectMessage(
                    sender_user_id=bob,
                    recipient_user_id=carol,
                    idempotency_key="dm-2",
                    kind="text",
                    text="not for this report",
                    price=0,
                    created_at=NOW,
                ),
                ChatRequest(
                    sender_user_id=alice,
                    recipient_user_id=bob,
                    idempotency_key="cr-1",
                    price=2,
                    hold_id=hold_id,
                    created_at=NOW,
                ),
            ]
        )
        await session.commit()
    report_id = await _report(session_factory, alice, bob, None)

    data = panel.get(f"/api/moderation/reports/{report_id}").json()

    assert data["kind"] == "profile"
    assert [(m["source"], m["sender"], m["kind"], m["viewable"]) for m in data["messages"]] == [
        ("direct", "reported", "photo", True)
    ]
    assert [(r["sender"], r["status"]) for r in data["requests"]] == [("reporter", "pending")]


async def test_a_deleted_account_shows_as_deleted(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    report_id, _ = await _chat_report(session_factory, people)
    async with session_factory() as session:
        await users.delete_account(session, people["bob"], NOW)
        await session.commit()

    data = panel.get(f"/api/moderation/reports/{report_id}").json()

    assert data["reported"]["deleted"] is True
    assert data["reported"]["nickname"] is None
    assert len(data["messages"]) == 5  # stored messages stay until retention


async def test_an_unknown_report_is_not_found(panel: Panel, people: dict[str, int]) -> None:
    assert panel.get("/api/moderation/reports/999").status_code == 404


# --- Media --------------------------------------------------------------------------


async def test_media_is_sent_protected_and_queued_for_deletion(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    report_id, ids = await _chat_report(session_factory, people)

    for item, method_type in ((ids["photo"], SendPhoto), (ids["video"], SendVideo)):
        response = panel.post(
            f"/api/moderation/reports/{report_id}/media", {"source": "chat", "item_id": item}
        )
        assert response.status_code == 200
        assert response.json() == {"deleted_after_seconds": 600}
        sent = panel.telegram.requests[-1]
        assert isinstance(sent, method_type)
        assert (sent.chat_id, sent.protect_content) == (MODERATOR, True)
    photo = panel.post(f"/api/moderation/reports/{report_id}/media", {"source": "photo"})
    assert photo.status_code == 200
    assert panel.telegram.requests[-1].photo == "AgAC-bob-photo"  # type: ignore[attr-defined]

    async with session_factory() as session:
        queued = (await session.scalars(select(TelegramDeletion))).all()
    assert len(queued) == 3
    for entry in queued:
        assert entry.chat_id == MODERATOR
        delay = entry.not_before - entry.created_at
        assert timedelta(minutes=9) < delay <= timedelta(minutes=10, seconds=5)
    assert [
        value for action, value in await _audit(session_factory) if action == "media_viewed"
    ] == [
        f"report {report_id} chat {ids['photo']}",
        f"report {report_id} chat {ids['video']}",
        f"report {report_id} photo",
    ]


async def test_media_outside_the_report_or_without_a_file_is_refused(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    report_id, ids = await _chat_report(session_factory, people)
    other_chat = await _conversation(session_factory, people["bob"], people["carol"])
    elsewhere = await _message(
        session_factory,
        other_chat,
        people["bob"],
        "photo",
        file_id="AgAC-else",
        sender_message_id=9,
    )
    # A stored message of a kind that can't be sent again, even with a file reference.
    unsendable = await _message(
        session_factory,
        await _conversation_of(session_factory, report_id),
        people["bob"],
        "other",
        file_id="XYZ-other",
        sender_message_id=10,
    )
    # A direct message between the same two people isn't part of a chat report.
    async with session_factory() as session:
        direct = DirectMessage(
            sender_user_id=people["bob"],
            recipient_user_id=people["alice"],
            idempotency_key="dm-chat-report",
            kind="photo",
            file_id="AgAC-direct",
            price=0,
            created_at=NOW,
        )
        session.add(direct)
        await session.commit()
        direct_id = direct.id
    for body in (
        {"source": "chat", "item_id": unsendable},
        {"source": "direct", "item_id": direct_id},
        {"source": "chat", "item_id": elsewhere},  # another chat's photo
        {"source": "chat", "item_id": ids["dice"]},  # no file
        {"source": "chat", "item_id": ids["hello"]},  # text
        {"source": "chat"},  # no item
        {"source": "direct", "item_id": ids["photo"]},  # wrong source for a chat report
    ):
        response = panel.post(f"/api/moderation/reports/{report_id}/media", body)
        assert response.status_code == 404, body
    assert (
        panel.post(f"/api/moderation/reports/{report_id}/media", {"source": "bogus"}).status_code
        == 422
    )
    assert panel.telegram.requests == []
    assert [a for a, _ in await _audit(session_factory)] == []


async def test_a_telegram_failure_is_reported_and_nothing_queued(
    panel: Panel, people: dict[str, int], session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_id, ids = await _chat_report(session_factory, people)

    async def refuse(*args: object, **kwargs: object) -> int:
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendPhoto as Method

        raise TelegramBadRequest(method=Method(chat_id=1, photo="x"), message="Bad Request")

    monkeypatch.setattr(moderation, "send_media", refuse)
    response = panel.post(
        f"/api/moderation/reports/{report_id}/media", {"source": "chat", "item_id": ids["photo"]}
    )

    assert (response.status_code, response.json()) == (502, {"error": "telegram"})
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TelegramDeletion)) == 0


# --- Actions ------------------------------------------------------------------------


async def test_an_action_closes_the_report(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    report_id, _ = await _chat_report(session_factory, people)

    response = panel.post(
        f"/api/moderation/reports/{report_id}/action",
        {"kind": "suspend", "rule": "harassment", "days": 7},
    )

    assert (response.status_code, response.json()) == (200, {"result": "applied"})
    async with session_factory() as session:
        report = await session.get(Report, report_id)
        bob = await session.get(User, people["bob"])
        assert report is not None and bob is not None
        assert (report.status, report.outcome) == ("closed", "action_taken")
        assert bob.status == "suspended"
        notices = await session.scalar(select(func.count()).select_from(ModerationNotice))
        assert notices == 2  # Bob and Alice
    assert panel.get("/api/moderation/reports").json() == []


async def test_a_closed_report_is_a_conflict(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    report_id, _ = await _chat_report(session_factory, people)
    assert (
        panel.post(f"/api/moderation/reports/{report_id}/action", {"kind": "close"}).status_code
        == 200
    )

    again = panel.post(
        f"/api/moderation/reports/{report_id}/action", {"kind": "ban", "rule": "spam"}, ADMIN
    )

    assert (again.status_code, again.json()) == (409, {"error": "report_closed"})
    async with session_factory() as session:
        bob = await session.get(User, people["bob"])
        assert bob is not None and bob.status == "active"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "warn"},  # no rule
        {"kind": "suspend", "rule": "spam"},  # no days
        {"kind": "suspend", "rule": "spam", "days": 2},
        {"kind": "ban", "rule": "made_up"},
        {"kind": "close", "rule": "spam"},
        {"kind": "lift"},  # not offered from a report
        {"kind": "warn", "rule": "spam", "extra": 1},
    ],
)
async def test_an_invalid_action_is_refused(
    panel: Panel, people: dict[str, int], session_factory: Sessions, body: dict[str, Any]
) -> None:
    report_id, _ = await _chat_report(session_factory, people)
    response = panel.post(f"/api/moderation/reports/{report_id}/action", body)
    assert response.status_code == 422
    async with session_factory() as session:
        report = await session.get(Report, report_id)
        assert report is not None and report.status == "open"


async def test_a_moderator_cannot_act_on_staff_from_a_report(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    report_id = await _report(session_factory, people["alice"], people["admin"], None)
    response = panel.post(
        f"/api/moderation/reports/{report_id}/action", {"kind": "warn", "rule": "spam"}
    )
    assert (response.status_code, response.json()) == (409, {"error": "protected"})


async def test_review_helpers_never_reach_another_report(
    people: dict[str, int], session_factory: Sessions
) -> None:
    # Directly, without the API: an item id from another report's chat finds nothing.
    first, ids = await _chat_report(session_factory, people)
    other = await _conversation(session_factory, people["carol"], people["bob"])
    second = await _report(session_factory, people["carol"], people["bob"], other)
    async with session_factory() as session:
        assert await review.media_for(session, second, review.Source.CHAT, ids["photo"]) is None
        assert await review.media_for(session, first, review.Source.CHAT, ids["photo"]) is not None


async def test_a_copy_whose_view_cannot_be_recorded_is_taken_back(
    panel: Panel, people: dict[str, int], session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_id, ids = await _chat_report(session_factory, people)

    real = review.record_media_view

    async def broken(*args: Any, **kwargs: Any) -> None:
        # The audit entry and the queued deletion are written, then it fails.
        await real(*args, **kwargs)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(review, "record_media_view", broken)
    with pytest.raises(RuntimeError):
        panel.post(
            f"/api/moderation/reports/{report_id}/media",
            {"source": "chat", "item_id": ids["photo"]},
        )

    sent = [m for m in panel.telegram.requests if isinstance(m, SendPhoto)]
    assert len(sent) == 1
    # The copy was deleted at once: nothing is left untracked in the moderator's chat.
    assert panel.telegram.deleted == [(MODERATOR, len(panel.telegram.requests) - 1)]
    # And nothing half-recorded is left: no queued deletion, no audit entry.
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TelegramDeletion)) == 0
    assert await _audit(session_factory) == []
