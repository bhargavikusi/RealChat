"""What moderators see when reviewing reports (D-071).

Like the other services, these take an AsyncSession and leave committing to the
caller. Content is reached only through a report: a chat report shows the stored
messages of its conversation, from both people; a profile report shows the direct
messages and chat requests between the two. A media item can be sent to a moderator
only if it belongs to the report. Every opening and every media view is written to the
audit log. Telegram names, usernames, phone numbers, and Telegram ids are never
included: RealChat stores none of the first three, and the last isn't needed.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from backend.modules.admin.models import AdminAuditEntry
from backend.modules.chat.models import ChatMessage, TelegramDeletion
from backend.modules.chat_requests.models import ChatRequest
from backend.modules.direct_messages.models import DirectMessage
from backend.modules.moderation.models import ModerationAction, Report
from backend.modules.users.models import Profile, User
from backend.modules.users.validation import current_age

QUEUE_LIMIT = 100
# The most messages a review shows, newest kept; retention keeps far fewer in practice.
MESSAGE_LIMIT = 2000
# How long media sent to a moderator stays in their chat (owner decision).
MEDIA_VIEW = timedelta(minutes=10)
# Telegram deletes a bot's message only within 48 hours of sending it.
DELETION_WINDOW = timedelta(hours=48)
# Kinds with a file that can be sent again by its Telegram reference. Polls, dice, and
# other kinds are shown as placeholders only.
SENDABLE_KINDS = (
    "photo",
    "video",
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "document",
)


@dataclass(frozen=True)
class QueueItem:
    id: int
    reason: str
    urgent: bool
    kind: str  # "chat" or "profile"
    created_at: datetime
    has_details: bool
    reported_nickname: str | None


async def open_reports(session: AsyncSession, limit: int = QUEUE_LIMIT) -> list[QueueItem]:
    """Open reports, urgent first, then oldest first. No content."""
    rows = await session.execute(
        select(
            Report.id,
            Report.reason,
            Report.urgent,
            Report.conversation_id,
            Report.created_at,
            Report.details,
            Profile.nickname,
        )
        .outerjoin(Profile, Profile.user_id == Report.reported_user_id)
        .where(Report.status == "open")
        .order_by(Report.urgent.desc(), Report.created_at, Report.id)
        .limit(limit)
    )
    return [
        QueueItem(
            id=report_id,
            reason=reason,
            urgent=urgent,
            kind="profile" if conversation_id is None else "chat",
            created_at=created_at,
            has_details=details is not None,
            reported_nickname=nickname,
        )
        for report_id, reason, urgent, conversation_id, created_at, details, nickname in rows.all()
    ]


@dataclass(frozen=True)
class Person:
    """A profile card as moderators see it. `deleted` means the account was deleted
    and hasn't come back, so the card is empty."""

    public_id: str | None
    nickname: str | None
    age: int | None
    gender: str | None
    country_code: str | None
    city: str | None
    bio: str | None
    has_photo: bool
    status: str
    deleted: bool


@dataclass(frozen=True)
class PastAction:
    kind: str
    rule: str | None
    days: int | None
    ends_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class Record:
    actions: list[PastAction]
    reports_received: int
    reports_open: int


@dataclass(frozen=True)
class Item:
    """One message in a review. `sender` is "reporter" or "reported"."""

    source: str  # "chat" or "direct"
    id: int
    sender: str
    kind: str
    text: str | None
    edited_text: str | None
    edited_at: datetime | None
    private: bool
    view_once: bool
    viewable: bool  # has a file that can be sent to the moderator
    sent_at: datetime


@dataclass(frozen=True)
class RequestItem:
    sender: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class Review:
    id: int
    kind: str
    reason: str
    urgent: bool
    details: str | None
    status: str
    outcome: str | None
    created_at: datetime
    reporter: Person
    reported: Person
    record: Record
    messages: list[Item]
    requests: list[RequestItem]


async def _person(session: AsyncSession, user_id: int, today: date) -> Person:
    user, profile = (
        await session.execute(
            select(User, Profile)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.id == user_id)
        )
    ).one()
    if profile is None or profile.nickname is None:
        return Person(None, None, None, None, None, None, None, False, user.status, True)
    age = (
        None
        if profile.age_at_entry is None or profile.age_entered_on is None
        else current_age(profile.age_at_entry, profile.age_entered_on, today)
    )
    return Person(
        public_id=profile.public_id,
        nickname=profile.nickname,
        age=age,
        gender=profile.gender,
        country_code=profile.country_code,
        city=profile.city,
        bio=profile.bio,
        has_photo=profile.photo_file_id is not None,
        status=user.status,
        deleted=False,
    )


async def _record(session: AsyncSession, user_id: int) -> Record:
    actions = await session.execute(
        select(
            ModerationAction.kind,
            ModerationAction.rule,
            ModerationAction.days,
            ModerationAction.ends_at,
            ModerationAction.created_at,
        )
        .where(ModerationAction.target_user_id == user_id, ModerationAction.kind != "close")
        .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
    )
    received, still_open = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Report.status == "open"),
            ).where(Report.reported_user_id == user_id)
        )
    ).one()
    return Record(
        actions=[PastAction(*row) for row in actions.all()],
        reports_received=int(received),
        reports_open=int(still_open),
    )


def _between(
    sender: InstrumentedAttribute[int], recipient: InstrumentedAttribute[int], a: int, b: int
) -> ColumnElement[bool]:
    """Sent by either person to the other."""
    return or_(and_(sender == a, recipient == b), and_(sender == b, recipient == a))


async def _chat_messages(
    session: AsyncSession, conversation_id: int, reporter_id: int
) -> list[Item]:
    rows = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(MESSAGE_LIMIT)
    )
    return [
        Item(
            source="chat",
            id=message.id,
            sender="reporter" if message.sender_user_id == reporter_id else "reported",
            kind=message.kind,
            text=message.text,
            edited_text=message.edited_text,
            edited_at=message.edited_at,
            private=message.private_session_id is not None,
            view_once=message.view_once,
            viewable=message.file_id is not None and message.kind in SENDABLE_KINDS,
            sent_at=message.created_at,
        )
        for message in reversed(rows.scalars().all())
    ]


async def _direct_messages(session: AsyncSession, reporter_id: int, reported_id: int) -> list[Item]:
    rows = await session.execute(
        select(DirectMessage)
        .where(
            _between(
                DirectMessage.sender_user_id,
                DirectMessage.recipient_user_id,
                reporter_id,
                reported_id,
            )
        )
        .order_by(DirectMessage.created_at.desc(), DirectMessage.id.desc())
        .limit(MESSAGE_LIMIT)
    )
    return [
        Item(
            source="direct",
            id=message.id,
            sender="reporter" if message.sender_user_id == reporter_id else "reported",
            kind=message.kind,
            text=message.text,
            edited_text=None,
            edited_at=None,
            private=False,
            view_once=False,
            viewable=message.file_id is not None and message.kind in SENDABLE_KINDS,
            sent_at=message.created_at,
        )
        for message in reversed(rows.scalars().all())
    ]


async def _requests(session: AsyncSession, reporter_id: int, reported_id: int) -> list[RequestItem]:
    rows = await session.execute(
        select(ChatRequest.sender_user_id, ChatRequest.status, ChatRequest.created_at)
        .where(
            _between(
                ChatRequest.sender_user_id, ChatRequest.recipient_user_id, reporter_id, reported_id
            )
        )
        .order_by(ChatRequest.created_at, ChatRequest.id)
    )
    return [
        RequestItem("reporter" if sender == reporter_id else "reported", status, created_at)
        for sender, status, created_at in rows.all()
    ]


def _audit(actor_user_id: int, action: str, target_user_id: int, value: str) -> AdminAuditEntry:
    return AdminAuditEntry(
        actor_user_id=actor_user_id, action=action, target_user_id=target_user_id, new_value=value
    )


async def review(
    session: AsyncSession, actor_user_id: int, report_id: int, today: date
) -> Review | None:
    """Everything a moderator needs to decide on a report, or None if there is no such
    report. The opening is written to the audit log."""
    report = await session.get(Report, report_id)
    if report is None:
        return None
    reporter, reported = report.reporter_user_id, report.reported_user_id
    if report.conversation_id is None:
        messages = await _direct_messages(session, reporter, reported)
        requests = await _requests(session, reporter, reported)
    else:
        messages = await _chat_messages(session, report.conversation_id, reporter)
        requests = []
    session.add(_audit(actor_user_id, "report_viewed", reported, f"report {report.id}"))
    return Review(
        id=report.id,
        kind="profile" if report.conversation_id is None else "chat",
        reason=report.reason,
        urgent=report.urgent,
        details=report.details,
        status=report.status,
        outcome=report.outcome,
        created_at=report.created_at,
        reporter=await _person(session, reporter, today),
        reported=await _person(session, reported, today),
        record=await _record(session, reported),
        messages=messages,
        requests=requests,
    )


class Source(StrEnum):
    """Where a media item comes from."""

    CHAT = "chat"  # a message in the reported chat
    DIRECT = "direct"  # a direct message between the two people
    PHOTO = "photo"  # the reported profile's photo


@dataclass(frozen=True)
class Media:
    kind: str
    file_id: str
    target_user_id: int


async def media_for(
    session: AsyncSession, report_id: int, source: Source, item_id: int | None
) -> Media | None:
    """The file to send for this item, only if it belongs to the report and can be
    sent again; None otherwise, so an id can't reach content outside the report."""
    report = await session.get(Report, report_id)
    if report is None:
        return None
    reporter, reported = report.reporter_user_id, report.reported_user_id
    if source is Source.PHOTO:
        file_id = await session.scalar(
            select(Profile.photo_file_id).where(Profile.user_id == reported)
        )
        return None if file_id is None else Media("photo", file_id, reported)
    if source is Source.CHAT:
        if report.conversation_id is None or item_id is None:
            return None
        row = (
            await session.execute(
                select(ChatMessage.kind, ChatMessage.file_id).where(
                    ChatMessage.id == item_id,
                    ChatMessage.conversation_id == report.conversation_id,
                    ChatMessage.file_id.is_not(None),
                    ChatMessage.kind.in_(SENDABLE_KINDS),
                )
            )
        ).one_or_none()
    else:
        if report.conversation_id is not None or item_id is None:
            return None
        row = (
            await session.execute(
                select(DirectMessage.kind, DirectMessage.file_id).where(
                    DirectMessage.id == item_id,
                    _between(
                        DirectMessage.sender_user_id,
                        DirectMessage.recipient_user_id,
                        reporter,
                        reported,
                    ),
                    DirectMessage.file_id.is_not(None),
                    DirectMessage.kind.in_(SENDABLE_KINDS),
                )
            )
        ).one_or_none()
    if row is None or row[1] is None:
        return None
    return Media(row[0], row[1], reported)


async def record_media_view(
    session: AsyncSession,
    actor_user_id: int,
    report_id: int,
    source: Source,
    item_id: int | None,
    media: Media,
    chat_id: int,
    message_id: int,
    now: datetime,
) -> None:
    """Audit a media view, and queue the copy sent to the moderator for deletion after
    MEDIA_VIEW (the worker's Telegram deletion job removes it)."""
    item = "" if item_id is None else f" {item_id}"
    session.add(
        _audit(
            actor_user_id,
            "media_viewed",
            media.target_user_id,
            f"report {report_id} {source}{item}",
        )
    )
    await session.execute(
        insert(TelegramDeletion)
        .values(
            chat_id=chat_id,
            message_id=message_id,
            not_before=now + MEDIA_VIEW,
            expires_at=now + DELETION_WINDOW,
        )
        .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
    )
