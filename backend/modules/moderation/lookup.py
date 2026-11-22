"""Looking up accounts from the moderation Mini App (D-071).

Like the other services, these take an AsyncSession and leave committing to the
caller. Moderators and admins can find an account by its RealChat ID (owner decision:
only the ID the bot shows, so a deleted account, whose ID is erased, can't be found)
and see its card, status, record, and reports; every lookup is audited. Only admins can
list or open the account's stored chats and direct messages, one thread at a time,
each time with a reason that is saved in the audit log. A thread or item can be opened
only if the account took part in it.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from backend.modules.chat.models import ChatMessage, ConversationParticipant
from backend.modules.direct_messages.models import DirectMessage
from backend.modules.moderation import review
from backend.modules.moderation.models import Report
from backend.modules.moderation.review import Item, Media, Person, Record, Source, audit
from backend.modules.users import public_ids
from backend.modules.users.models import Profile, User

ACCOUNT_LABELS: review.Labels = ("account", "partner")
REPORTS_LIMIT = 100
# The ID as the bot shows it is accepted too: /userid_<id>, userid_<id>, /userid <id>.
USERID_PREFIX = re.compile(r"^/?userid[_ ]\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Match:
    id: int
    public_id: str | None
    nickname: str | None
    status: str


async def find(session: AsyncSession, actor_user_id: int, query: str) -> list[Match]:
    """The account with this RealChat ID (as typed, or as the bot shows it), as a list
    of zero or one. Restricted accounts are found; a deleted account isn't, since its
    RealChat ID is erased with its profile. The lookup is audited, found or not."""
    public_id = public_ids.normalize(USERID_PREFIX.sub("", query.strip()))
    row = None
    if public_id is not None:
        row = (
            await session.execute(
                select(User.id, Profile.public_id, Profile.nickname, User.status)
                .join(Profile, Profile.user_id == User.id)
                .where(Profile.public_id == public_id)
            )
        ).one_or_none()
    match = None if row is None else Match(row[0], row[1], row[2], row[3])
    if public_id is None:
        outcome = "not a RealChat ID"
    else:
        outcome = "found" if match else "not found"
    session.add(
        audit(actor_user_id, "account_looked_up", None if match is None else match.id, outcome)
    )
    return [] if match is None else [match]


@dataclass(frozen=True)
class ReportSummary:
    id: int
    reason: str
    kind: str  # "chat" or "profile"
    status: str
    outcome: str | None
    created_at: datetime


@dataclass(frozen=True)
class Account:
    id: int
    person: Person
    suspended_until: datetime | None
    record: Record
    reports_received: list[ReportSummary]
    reports_made: list[ReportSummary]


async def _reports(
    session: AsyncSession, column: InstrumentedAttribute[int], user_id: int
) -> list[ReportSummary]:
    rows = await session.execute(
        select(
            Report.id,
            Report.reason,
            Report.conversation_id,
            Report.status,
            Report.outcome,
            Report.created_at,
        )
        .where(column == user_id)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(REPORTS_LIMIT)
    )
    return [
        ReportSummary(
            report_id, reason, "profile" if conversation is None else "chat", status, outcome, at
        )
        for report_id, reason, conversation, status, outcome, at in rows.all()
    ]


async def account(session: AsyncSession, user_id: int, today: date) -> Account | None:
    """An account's card, status, record, and the reports it received and made."""
    suspended_until = (
        await session.execute(select(User.suspended_until).where(User.id == user_id))
    ).one_or_none()
    if suspended_until is None:
        return None
    return Account(
        id=user_id,
        person=await review.person(session, user_id, today),
        suspended_until=suspended_until[0],
        record=await review.record(session, user_id),
        reports_received=await _reports(session, Report.reported_user_id, user_id),
        reports_made=await _reports(session, Report.reporter_user_id, user_id),
    )


@dataclass(frozen=True)
class Thread:
    """A stored conversation, or the direct messages with one person."""

    kind: str  # "chat" or "direct"
    ref: int  # the conversation's id, or the other person's account id
    partner_nickname: str | None
    first_at: datetime
    last_at: datetime
    messages: int


async def threads(
    session: AsyncSession, actor_user_id: int, user_id: int, reason: str
) -> list[Thread]:
    """The account's stored conversations and direct-message threads, newest first, with
    no content. Audited with the admin's reason."""
    partner = ConversationParticipant.__table__.alias("partner")
    chats = await session.execute(
        select(
            ChatMessage.conversation_id,
            Profile.nickname,
            func.min(ChatMessage.created_at),
            func.max(ChatMessage.created_at),
            func.count(),
        )
        .join(
            ConversationParticipant,
            (ConversationParticipant.conversation_id == ChatMessage.conversation_id)
            & (ConversationParticipant.user_id == user_id),
        )
        .outerjoin(
            partner,
            (partner.c.conversation_id == ChatMessage.conversation_id)
            & (partner.c.user_id != user_id),
        )
        .outerjoin(Profile, Profile.user_id == partner.c.user_id)
        .group_by(ChatMessage.conversation_id, Profile.nickname)
    )
    other = (
        func.least(DirectMessage.sender_user_id, DirectMessage.recipient_user_id)
        + func.greatest(DirectMessage.sender_user_id, DirectMessage.recipient_user_id)
        - user_id
    )
    directs = await session.execute(
        select(
            other,
            func.min(DirectMessage.created_at),
            func.max(DirectMessage.created_at),
            func.count(),
        )
        .where(
            (DirectMessage.sender_user_id == user_id) | (DirectMessage.recipient_user_id == user_id)
        )
        .group_by(other)
    )
    found = [
        Thread("chat", ref, nickname, first, last, count)
        for ref, nickname, first, last, count in chats.all()
    ]
    direct_rows = directs.all()
    nicknames = dict(
        (
            await session.execute(
                select(Profile.user_id, Profile.nickname).where(
                    Profile.user_id.in_([row[0] for row in direct_rows])
                )
            )
        ).all()
    )
    found += [
        Thread("direct", ref, nicknames.get(ref), first, last, count)
        for ref, first, last, count in direct_rows
    ]
    found.sort(key=lambda thread: thread.last_at, reverse=True)
    session.add(audit(actor_user_id, "chats_listed", user_id, f"account {user_id}", reason))
    return found


async def _in_conversation(session: AsyncSession, user_id: int, conversation_id: int) -> bool:
    found = await session.scalar(
        select(ConversationParticipant.user_id).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
    )
    return found is not None


async def open_thread(
    session: AsyncSession, actor_user_id: int, user_id: int, kind: str, ref: int, reason: str
) -> list[Item] | None:
    """One thread's messages, oldest first, or None if the account took no part in it.
    Audited with the admin's reason."""
    if kind == "chat":
        if not await _in_conversation(session, user_id, ref):
            return None
        items = await review.chat_messages(session, ref, user_id, ACCOUNT_LABELS)
    elif kind == "direct":
        # No messages means no thread (nobody can message themselves, by constraint).
        items = await review.direct_messages(session, user_id, ref, ACCOUNT_LABELS)
        if not items:
            return None
    else:
        return None
    session.add(
        audit(actor_user_id, "chat_opened", user_id, f"account {user_id} {kind} {ref}", reason)
    )
    return items


async def media_for(
    session: AsyncSession, user_id: int, source: Source, item_id: int
) -> Media | None:
    """The file for an item in one of the account's threads, only if the account took
    part in it and the file can be sent again; None otherwise."""
    if source is Source.CHAT:
        row = (
            await session.execute(
                select(ChatMessage.kind, ChatMessage.file_id)
                .join(
                    ConversationParticipant,
                    (ConversationParticipant.conversation_id == ChatMessage.conversation_id)
                    & (ConversationParticipant.user_id == user_id),
                )
                .where(
                    ChatMessage.id == item_id,
                    ChatMessage.file_id.is_not(None),
                    ChatMessage.kind.in_(review.SENDABLE_KINDS),
                )
            )
        ).one_or_none()
    elif source is Source.DIRECT:
        row = (
            await session.execute(
                select(DirectMessage.kind, DirectMessage.file_id).where(
                    DirectMessage.id == item_id,
                    (DirectMessage.sender_user_id == user_id)
                    | (DirectMessage.recipient_user_id == user_id),
                    DirectMessage.file_id.is_not(None),
                    DirectMessage.kind.in_(review.SENDABLE_KINDS),
                )
            )
        ).one_or_none()
    else:
        return None
    if row is None or row[1] is None:
        return None
    return Media(row[0], row[1], user_id)
