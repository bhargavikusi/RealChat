"""Conversation operations shared by the bot and the worker.

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids here are internal `users.id` values, never Telegram IDs.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.modules.chat.models import ChatMessage, Conversation, ConversationParticipant
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
    await _close(session, conversation, now, reason, user_id if reason == "user_ended" else None)
    return chat


async def _close(
    session: AsyncSession,
    conversation: Conversation,
    now: datetime,
    reason: str,
    ended_by: int | None,
) -> None:
    """End a conversation whose row the caller has locked."""
    conversation.ended_at = now
    conversation.end_reason = reason
    conversation.ended_by_user_id = ended_by
    await session.execute(
        update(Participant)
        .where(Participant.conversation_id == conversation.id)
        .values(left_at=now)
    )


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


# --- Relayed messages (D-028, D-038) -------------------------------------------------


async def relayed_message(
    session: AsyncSession, sender_id: int, sender_message_id: int
) -> ChatMessage | None:
    """The stored record of a message the sender sent, if it was relayed."""
    return (
        await session.execute(
            select(ChatMessage).where(
                ChatMessage.sender_user_id == sender_id,
                ChatMessage.sender_message_id == sender_message_id,
            )
        )
    ).scalar_one_or_none()


async def reply_target(
    session: AsyncSession, chat: ActiveChat, user_id: int, replied_message_id: int
) -> int | None:
    """Where a reply should point in the partner's chat, or None if it can't be mapped.

    In the user's own chat, the replied-to message is either one they sent (the partner
    has a copy of it) or a copy of one the partner sent (the partner has the original).
    """
    row = (
        await session.execute(
            select(
                ChatMessage.sender_user_id,
                ChatMessage.sender_message_id,
                ChatMessage.recipient_message_id,
            ).where(
                ChatMessage.conversation_id == chat.conversation_id,
                ChatMessage.recipient_message_id.is_not(None),
                or_(
                    and_(
                        ChatMessage.sender_user_id == user_id,
                        ChatMessage.sender_message_id == replied_message_id,
                    ),
                    and_(
                        ChatMessage.sender_user_id == chat.partner_id,
                        ChatMessage.recipient_message_id == replied_message_id,
                    ),
                ),
            )
        )
    ).first()
    if row is None:
        return None
    sender, sender_message_id, recipient_message_id = row
    target = recipient_message_id if sender == user_id else sender_message_id
    return None if target is None else int(target)


async def _conversation_open(session: AsyncSession, conversation_id: int) -> bool:
    """Whether the conversation is open, share-locking its row until the transaction ends.

    `end_conversation` takes the row's update lock, so an end in progress is waited for
    and then seen.
    """
    ended = (
        await session.execute(
            select(Conversation.ended_at)
            .where(Conversation.id == conversation_id)
            .with_for_update(read=True)
        )
    ).scalar_one()
    return ended is None


async def claim_message(
    session: AsyncSession,
    chat: ActiveChat,
    sender_id: int,
    sender_message_id: int,
    kind: str,
    text: str | None,
    file_id: str | None,
) -> int | None:
    """Record a message before it is copied; returns its row id.

    Returns None if it was already recorded (a redelivered update) or the conversation
    has ended. Recording first means a message is relayed at most once, even if the bot
    stops between sending the copy and saving it.
    """
    # Locked for update (not shared): the chat's activity is recorded below, and the
    # idle ending locks the same row, so a message and an idle ending never interleave.
    still_open = await session.scalar(
        update(Conversation)
        .where(Conversation.id == chat.conversation_id, Conversation.ended_at.is_(None))
        .values(last_activity_at=func.now(), idle_warned_at=None)
        .returning(Conversation.id)
    )
    if still_open is None:
        return None
    row_id = await session.scalar(
        insert(ChatMessage)
        .values(
            conversation_id=chat.conversation_id,
            sender_user_id=sender_id,
            sender_message_id=sender_message_id,
            kind=kind,
            text=text,
            file_id=file_id,
        )
        .on_conflict_do_nothing(index_elements=["sender_user_id", "sender_message_id"])
        .returning(ChatMessage.id)
    )
    return None if row_id is None else int(row_id)


async def complete_message(
    session: AsyncSession, row_id: int, conversation_id: int, recipient_message_id: int
) -> bool:
    """Link a recorded message to its delivered copy.

    Returns False, and removes the record, if the conversation ended while the copy was
    on its way; the caller then withdraws the copy.
    """
    if not await _conversation_open(session, conversation_id):
        await discard_message(session, row_id)
        return False
    await session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == row_id)
        .values(recipient_message_id=recipient_message_id)
    )
    return True


async def discard_message(session: AsyncSession, row_id: int) -> None:
    """Remove a recorded message that was not delivered."""
    await session.execute(delete(ChatMessage).where(ChatMessage.id == row_id))


def record_edit(message: ChatMessage, text: str, now: datetime) -> None:
    """Keep the original text and store the latest edit beside it (D-043)."""
    message.edited_text = text
    message.edited_at = now


# --- Idle chats (D-039) --------------------------------------------------------------


@dataclass(frozen=True)
class IdleChat:
    conversation_id: int
    user_ids: tuple[int, int]


async def _participants(session: AsyncSession, conversation_ids: list[int]) -> list[IdleChat]:
    if not conversation_ids:
        return []
    rows = await session.execute(
        select(Participant.conversation_id, Participant.user_id)
        .where(Participant.conversation_id.in_(conversation_ids))
        .order_by(Participant.conversation_id, Participant.user_id)
    )
    members: dict[int, list[int]] = {}
    for conversation_id, user_id in rows:
        members.setdefault(conversation_id, []).append(user_id)
    return [
        IdleChat(conversation_id, (users[0], users[1]))
        for conversation_id, users in members.items()
        if len(users) == 2
    ]


async def warn_idle_chats(
    session: AsyncSession, now: datetime, quiet_for: timedelta, limit: int
) -> list[IdleChat]:
    """Mark open chats quiet for `quiet_for` as warned; returns them, to tell both users.

    Each chat is warned once: the update only matches chats not yet warned, and rows
    another worker is handling are skipped.
    """
    candidates = (
        select(Conversation.id)
        .where(
            Conversation.ended_at.is_(None),
            Conversation.idle_warned_at.is_(None),
            Conversation.last_activity_at <= now - quiet_for,
        )
        .order_by(Conversation.last_activity_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    warned = await session.execute(
        update(Conversation)
        .where(Conversation.id.in_(candidates.scalar_subquery()))
        .values(idle_warned_at=now)
        .returning(Conversation.id)
    )
    return await _participants(session, list(warned.scalars()))


async def end_idle_chats(
    session: AsyncSession, now: datetime, grace: timedelta, limit: int
) -> list[IdleChat]:
    """End chats warned at least `grace` ago with no message since; returns them.

    A relayed message clears the warning under the same row lock, so a chat with a
    message after its warning is never ended here.
    """
    conversations = (
        (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.ended_at.is_(None),
                    Conversation.idle_warned_at.is_not(None),
                    Conversation.idle_warned_at <= now - grace,
                )
                .order_by(Conversation.idle_warned_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for conversation in conversations:
        await _close(session, conversation, now, "idle", None)
    return await _participants(session, [c.id for c in conversations])
