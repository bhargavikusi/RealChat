"""Blocking and reporting chat partners (D-037, D-045).

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids are internal `users.id` values. Every action names a conversation,
and only the other participant of a conversation the user was in can be blocked or
reported, so nobody can act on an account they were never matched with.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.modules.chat import service as chat
from backend.modules.chat.models import Conversation
from backend.modules.chat.models import ConversationParticipant as Participant
from backend.modules.moderation.models import (
    REPORT_DETAILS_MAX_LENGTH,
    REPORT_REASONS,
    URGENT_REASONS,
    Report,
    UserBlock,
)
from backend.modules.users.models import User

REPORT_PERIOD = timedelta(hours=24)


class NotPartnerError(Exception):
    """The user was not in this conversation, so has no partner in it to act on."""


async def partner_in(session: AsyncSession, user_id: int, conversation_id: int) -> int:
    """The other participant of a conversation the user was in.

    Raises NotPartnerError otherwise, including for a conversation that doesn't exist.
    """
    other = aliased(Participant)
    partner = (
        await session.execute(
            select(other.user_id)
            .join(
                Participant,
                and_(
                    Participant.conversation_id == other.conversation_id,
                    Participant.user_id == user_id,
                ),
            )
            .where(other.conversation_id == conversation_id, other.user_id != user_id)
        )
    ).scalar_one_or_none()
    if partner is None:
        raise NotPartnerError
    return int(partner)


async def is_blocked(session: AsyncSession, blocker_id: int, blocked_id: int) -> bool:
    """Whether `blocker_id` has a block in force on `blocked_id`."""
    found = await session.scalar(
        select(UserBlock.id).where(
            UserBlock.blocker_user_id == blocker_id,
            UserBlock.blocked_user_id == blocked_id,
            UserBlock.unblocked_at.is_(None),
        )
    )
    return found is not None


async def _add_block(
    session: AsyncSession, blocker_id: int, blocked_id: int, conversation_id: int
) -> bool:
    """Store a block unless one is already in force; returns whether it was added."""
    added = await session.scalar(
        insert(UserBlock)
        .values(
            blocker_user_id=blocker_id, blocked_user_id=blocked_id, conversation_id=conversation_id
        )
        .on_conflict_do_nothing(
            index_elements=["blocker_user_id", "blocked_user_id"],
            index_where=UserBlock.unblocked_at.is_(None),
        )
        .returning(UserBlock.id)
    )
    return added is not None


async def _lock(
    session: AsyncSession,
    user_id: int,
    partner_id: int,
    conversation_id: int,
    *,
    serialize_user: bool = False,
) -> None:
    """Take every lock a block or report needs, up front and always in the same order.

    Users first, then the conversation, as account deletion does (it locks the user,
    then ends their chat). The partner's row gets the key-share lock that the new
    rows' foreign keys would take anyway; taking it before the conversation lock means
    a block or report never holds the conversation while waiting on a user, so two
    users acting on the same chat at once, or one acting while the other deletes their
    account, cannot deadlock. With `serialize_user`, the user's own row is locked FOR
    NO KEY UPDATE, so one user's simultaneous reports are counted one at a time;
    it does not conflict with key-share locks taken by reports about that user.
    """
    own = select(User.id).where(User.id == user_id)
    await session.execute(
        own.with_for_update(key_share=True)
        if serialize_user
        else own.with_for_update(read=True, key_share=True)
    )
    await session.execute(
        select(User.id).where(User.id == partner_id).with_for_update(read=True, key_share=True)
    )
    await session.execute(
        select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
    )


@dataclass(frozen=True)
class Blocked:
    partner_id: int
    added: bool  # False if the partner was already blocked
    ended_chat: bool  # True if this ended the open conversation with the partner


async def block(
    session: AsyncSession, user_id: int, conversation_id: int, now: datetime
) -> Blocked:
    """Block the user's partner in `conversation_id`, ending that conversation if open.

    Raises NotPartnerError if the user was not in it. Repeated or concurrent calls
    store one block, and only one of them ends the conversation.
    """
    partner_id = await partner_in(session, user_id, conversation_id)
    await _lock(session, user_id, partner_id, conversation_id)
    added = await _add_block(session, user_id, partner_id, conversation_id)
    ended = await chat.end_conversation(
        session, user_id, now, "blocked", conversation_id=conversation_id
    )
    return Blocked(partner_id, added, ended is not None)


async def unblock(session: AsyncSession, user_id: int, conversation_id: int, now: datetime) -> int:
    """Lift the user's block on their partner in `conversation_id`; returns the partner.

    Raises NotPartnerError if the user was not in it. Does nothing if there is no block.
    """
    partner_id = await partner_in(session, user_id, conversation_id)
    await session.execute(
        update(UserBlock)
        .where(
            UserBlock.blocker_user_id == user_id,
            UserBlock.blocked_user_id == partner_id,
            UserBlock.unblocked_at.is_(None),
        )
        .values(unblocked_at=now)
    )
    return partner_id


class ReportOutcome(StrEnum):
    FILED = "filed"
    ALREADY_REPORTED = "already_reported"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True)
class Filed:
    outcome: ReportOutcome
    partner_id: int
    report_id: int | None = None  # set when filed
    ended_chat: bool = False  # True if filing ended the open conversation


async def has_reported(session: AsyncSession, user_id: int, conversation_id: int) -> bool:
    found = await session.scalar(
        select(Report.id).where(
            Report.reporter_user_id == user_id, Report.conversation_id == conversation_id
        )
    )
    return found is not None


async def file_report(
    session: AsyncSession,
    user_id: int,
    conversation_id: int,
    reason: str,
    now: datetime,
    per_day: int,
) -> Filed:
    """Report the user's partner in `conversation_id`, block them, and end the chat if open.

    Raises NotPartnerError if the user was not in it, and ValueError for an unknown
    reason. A user reports a conversation at most once, and files at most `per_day`
    reports in 24 hours; the user's row is locked while counting, so simultaneous
    reports cannot pass the limit together (see `_lock` for the lock order).
    """
    if reason not in REPORT_REASONS:
        raise ValueError(f"unknown report reason: {reason}")
    partner_id = await partner_in(session, user_id, conversation_id)
    await _lock(session, user_id, partner_id, conversation_id, serialize_user=True)
    if await has_reported(session, user_id, conversation_id):
        return Filed(ReportOutcome.ALREADY_REPORTED, partner_id)
    recent = await session.scalar(
        select(func.count())
        .select_from(Report)
        .where(Report.reporter_user_id == user_id, Report.created_at > now - REPORT_PERIOD)
    )
    if (recent or 0) >= per_day:
        return Filed(ReportOutcome.LIMIT_REACHED, partner_id)
    report = Report(
        reporter_user_id=user_id,
        reported_user_id=partner_id,
        conversation_id=conversation_id,
        reason=reason,
        urgent=reason in URGENT_REASONS,
        created_at=now,
    )
    session.add(report)
    await session.flush()
    await _add_block(session, user_id, partner_id, conversation_id)
    ended = await chat.end_conversation(
        session, user_id, now, "reported", conversation_id=conversation_id
    )
    return Filed(ReportOutcome.FILED, partner_id, report.id, ended is not None)


def clean_details(raw: str) -> str:
    """Report details as stored: trimmed, 1 to 500 characters. Raises ValueError otherwise."""
    details = raw.strip()
    if not details or len(details) > REPORT_DETAILS_MAX_LENGTH:
        raise ValueError("report details must be 1 to 500 characters")
    return details


async def add_details(session: AsyncSession, user_id: int, report_id: int, raw: str) -> bool:
    """Attach details to the user's own report, once; returns whether they were added.

    Raises ValueError if the details are empty or too long.
    """
    details = clean_details(raw)
    result = await session.execute(
        update(Report)
        .where(
            Report.id == report_id,
            Report.reporter_user_id == user_id,
            Report.details.is_(None),
        )
        .values(details=details)
        .returning(Report.id)
    )
    return result.scalar_one_or_none() is not None
