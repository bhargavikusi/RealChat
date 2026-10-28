"""Conversation operations shared by the bot and the worker.

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids here are internal `users.id` values, never Telegram IDs.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.modules.chat.models import Conversation, ConversationParticipant
from backend.modules.users.models import Profile, TermsAcceptance, User

Participant = ConversationParticipant


@dataclass(frozen=True)
class ActiveChat:
    conversation_id: int
    partner_id: int


async def active_chat(session: AsyncSession, user_id: int) -> ActiveChat | None:
    """The user's open conversation and partner, if any."""
    partner = aliased(Participant)
    row = (
        await session.execute(
            select(Participant.conversation_id, partner.user_id)
            .join(
                partner,
                and_(
                    partner.conversation_id == Participant.conversation_id,
                    partner.user_id != Participant.user_id,
                ),
            )
            .where(Participant.user_id == user_id, Participant.left_at.is_(None))
        )
    ).one_or_none()
    return None if row is None else ActiveChat(conversation_id=row[0], partner_id=row[1])


async def start_conversation(session: AsyncSession, first: int, second: int) -> int:
    """Create an open conversation between two users; returns its id.

    Raises IntegrityError (at flush) if either user is already in an open conversation:
    the partial unique index on participants is the final guard against double matches.
    """
    if first == second:
        raise ValueError("a user cannot be matched with themselves")
    conversation = Conversation()
    session.add(conversation)
    await session.flush()
    session.add_all(
        [
            Participant(conversation_id=conversation.id, user_id=first),
            Participant(conversation_id=conversation.id, user_id=second),
        ]
    )
    await session.flush()
    return conversation.id


async def end_conversation(
    session: AsyncSession, user_id: int, now: datetime, reason: str = "user_ended"
) -> ActiveChat | None:
    """End the user's open conversation; returns it, or None if there was none.

    The conversation row is locked and re-read, so when both users end the chat at
    the same moment, exactly one call ends it and the other returns None.
    """
    chat = await active_chat(session, user_id)
    if chat is None:
        return None
    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.id == chat.conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if conversation.ended_at is not None:
        return None
    conversation.ended_at = now
    conversation.end_reason = reason
    conversation.ended_by_user_id = user_id if reason == "user_ended" else None
    await session.execute(
        update(Participant)
        .where(Participant.conversation_id == chat.conversation_id)
        .values(left_at=now)
    )
    return chat


async def eligible_for_matching(
    session: AsyncSession, user_ids: Collection[int], terms_version: str, *, lock: bool = False
) -> set[int]:
    """Those of `user_ids` who may be matched right now.

    Eligible means: an active account (not suspended, banned, or deleted), completed
    onboarding, the current terms accepted, and no open conversation. With `lock`, the
    eligible users' rows are share-locked until the transaction ends, so a concurrent
    account deletion (which takes the row's update lock) waits for the match.
    """
    if not user_ids:
        return set()
    open_conversation = exists().where(
        Participant.user_id == User.id, Participant.left_at.is_(None)
    )
    accepted = exists().where(
        TermsAcceptance.user_id == User.id, TermsAcceptance.terms_version == terms_version
    )
    query = (
        select(User.id)
        .join(Profile, Profile.user_id == User.id)
        .where(
            User.id.in_(user_ids),
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
            accepted,
            ~open_conversation,
        )
    )
    if lock:
        query = query.with_for_update(read=True, of=User)
    return set((await session.execute(query)).scalars())


async def recent_pairs(
    session: AsyncSession, user_ids: Collection[int], since: datetime
) -> set[frozenset[int]]:
    """Pairs among `user_ids` who shared a conversation that is open or ended at/after `since`."""
    if len(user_ids) < 2:
        return set()
    first, second = aliased(Participant), aliased(Participant)
    rows = await session.execute(
        select(first.user_id, second.user_id)
        .join(
            second,
            and_(second.conversation_id == first.conversation_id, second.user_id > first.user_id),
        )
        .join(Conversation, Conversation.id == first.conversation_id)
        .where(
            first.user_id.in_(user_ids),
            second.user_id.in_(user_ids),
            or_(Conversation.ended_at.is_(None), Conversation.ended_at >= since),
        )
    )
    return {frozenset((first_id, second_id)) for first_id, second_id in rows}
