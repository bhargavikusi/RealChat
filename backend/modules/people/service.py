"""People a user can reach again: their past chat partners (D-051).

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids are internal `users.id` values. A past chat partner is the other
participant of a conversation that ended within the window (an admin setting).
Only partners who can still use RealChat are included: an active account with
onboarding complete. Conversations from before either user deleted their account are
never counted, because a returning account starts afresh. Someone the user is in an
open chat with is left out, since that chat is not over. Blocks hide nobody; the
blocker's entry is only marked.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, Select, and_, exists, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.modules.chat.models import Conversation
from backend.modules.chat.models import ConversationParticipant as Participant
from backend.modules.moderation.models import UserBlock
from backend.modules.users.models import Profile, User

PAGE_SIZE = 10


@dataclass(frozen=True)
class PastChat:
    partner_id: int
    conversation_id: int  # the latest conversation with this partner in the window
    ended_at: datetime
    nickname: str
    blocked: bool  # whether the viewer has blocked this partner


@dataclass(frozen=True)
class PastChats:
    entries: list[PastChat]
    has_next: bool


def _past_conversations(viewer_id: int, since: datetime) -> Select[int, int, datetime | None]:
    """(partner, conversation, ended_at) for every conversation of the viewer's that
    counts as a past chat: ended at/after `since`, after both users' latest account
    deletion, with a partner who can still use RealChat and no open chat between them."""
    me, other = aliased(Participant), aliased(Participant)
    viewer, partner = aliased(User), aliased(User)
    mine, theirs = aliased(Participant), aliased(Participant)
    chatting_now = exists().where(
        mine.user_id == viewer_id,
        mine.left_at.is_(None),
        theirs.conversation_id == mine.conversation_id,
        theirs.user_id == other.user_id,
        theirs.left_at.is_(None),
    )
    return (
        select(other.user_id, Conversation.id, Conversation.ended_at)
        .join(me, and_(me.conversation_id == other.conversation_id, me.user_id == viewer_id))
        .join(Conversation, Conversation.id == other.conversation_id)
        .join(viewer, viewer.id == viewer_id)
        .join(partner, partner.id == other.user_id)
        .join(Profile, Profile.user_id == other.user_id)
        .where(
            other.user_id != viewer_id,
            Conversation.ended_at >= since,
            or_(viewer.deleted_at.is_(None), Conversation.started_at > viewer.deleted_at),
            or_(partner.deleted_at.is_(None), Conversation.started_at > partner.deleted_at),
            partner.status == "active",
            Profile.onboarding_completed_at.is_not(None),
            ~chatting_now,
        )
    )


def _blocked_by(viewer_id: int, partner_id: ColumnElement[int]) -> ColumnElement[bool]:
    return exists().where(
        UserBlock.blocker_user_id == viewer_id,
        UserBlock.blocked_user_id == partner_id,
        UserBlock.unblocked_at.is_(None),
    )


async def past_chats(
    session: AsyncSession, viewer_id: int, since: datetime, page: int = 0
) -> PastChats:
    """One page of the viewer's past chat partners, latest chat first, each listed once."""
    rows = _past_conversations(viewer_id, since).subquery()
    latest = (
        select(rows)
        .ext(distinct_on(rows.c.user_id))
        .order_by(rows.c.user_id, rows.c.ended_at.desc(), rows.c.id.desc())
        .subquery()
    )
    query = (
        select(
            latest.c.user_id,
            latest.c.id,
            latest.c.ended_at,
            Profile.nickname,
            _blocked_by(viewer_id, latest.c.user_id),
        )
        .join(Profile, Profile.user_id == latest.c.user_id)
        .order_by(latest.c.ended_at.desc(), latest.c.id.desc())
        .offset(max(page, 0) * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    )
    found = [
        PastChat(int(partner), int(conversation), ended_at, str(nickname), bool(blocked))
        for partner, conversation, ended_at, nickname, blocked in await session.execute(query)
    ]
    return PastChats(found[:PAGE_SIZE], has_next=len(found) > PAGE_SIZE)


async def past_partner(
    session: AsyncSession, viewer_id: int, conversation_id: int, since: datetime
) -> int | None:
    """The partner in `conversation_id`, if it counts as one of the viewer's past chats.

    None for a conversation the viewer was not in, one outside the window, or a partner
    who is no longer listed, so an old or forged button opens nobody.
    """
    rows = _past_conversations(viewer_id, since).where(Conversation.id == conversation_id)
    partner = (await session.execute(rows)).first()
    return None if partner is None else int(partner.user_id)


def window_start(now: datetime, days: int) -> datetime:
    """The earliest end time of a conversation that still counts as a past chat."""
    return now - timedelta(days=days)
