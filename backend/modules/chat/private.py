"""Private mode (D-031, D-047).

While Private mode is on in a conversation, every relayed message is linked to the
active session. When the session ends (a user turns it off, the chat ends, or the time
limit is reached), the sender's original and the partner's copy of each of those
messages are queued in `telegram_deletions`, and the worker deletes them from both
users' Telegram chats. Stored content keeps the normal retention for moderation.

Telegram lets bots delete a message for 48 hours after it was sent, and a dice message
in a private chat only once it is more than 24 hours old (confirmed against the Bot API
on 2026-10-30), so each queued deletion has a window: dice wait until they are past 24
hours, and nothing is tried after 48. The time limit (at most 46 hours) keeps every
message inside that window.

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids are internal `users.id` values.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import ColumnElement, and_, case, delete, exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat.models import (
    ChatMessage,
    Conversation,
    ConversationParticipant,
    PrivateSession,
    TelegramDeletion,
)
from backend.modules.users.models import User

logger = logging.getLogger(__name__)

# Telegram's limits for deleting a message in a private chat.
DELETE_WITHIN = timedelta(hours=48)
DICE_DELETABLE_AFTER = timedelta(hours=24)
# Margin after the dice limit, so the first attempt is not refused for being early.
DICE_MARGIN = timedelta(minutes=5)
# How long a claimed deletion is held by the worker trying it before it can be retried.
CLAIM_LEASE = timedelta(minutes=1)


async def active_session(session: AsyncSession, conversation_id: int) -> int | None:
    """The id of the conversation's active Private mode session, if any."""
    return await session.scalar(
        select(PrivateSession.id).where(
            PrivateSession.conversation_id == conversation_id, PrivateSession.ended_at.is_(None)
        )
    )


async def _lock_open_conversation(session: AsyncSession, conversation_id: int) -> bool:
    """Lock the conversation row for update; returns whether it is still open.

    The relay records each message under the same lock, so a message is either linked to
    a session before it ends or recorded after it, never in between.
    """
    ended = await session.execute(
        select(Conversation.ended_at).where(Conversation.id == conversation_id).with_for_update()
    )
    return ended.scalar_one() is None


@dataclass(frozen=True)
class Toggled:
    changed: bool  # False if it was already in the requested state
    has_dice: bool = False  # turning off: dice are deleted later (D-047)


async def turn_on(
    session: AsyncSession, conversation_id: int, user_id: int, now: datetime
) -> Toggled | None:
    """Turn Private mode on in the user's chat; None if the chat has ended."""
    if not await _lock_open_conversation(session, conversation_id):
        return None
    if await active_session(session, conversation_id) is not None:
        return Toggled(changed=False)
    session.add(
        PrivateSession(conversation_id=conversation_id, started_by_user_id=user_id, started_at=now)
    )
    await session.flush()
    return Toggled(changed=True)


async def turn_off(
    session: AsyncSession, conversation_id: int, user_id: int, now: datetime
) -> Toggled | None:
    """Turn Private mode off in the user's chat and queue its messages for deletion.

    None if the chat has ended (its session is then ended by the worker).
    """
    if not await _lock_open_conversation(session, conversation_id):
        return None
    private_id = await active_session(session, conversation_id)
    if private_id is None:
        return Toggled(changed=False)
    ended = await end_session(session, private_id, now, "turned_off", ended_by=user_id)
    if ended is None:  # the worker ended it meanwhile
        return Toggled(changed=False)
    return Toggled(changed=True, has_dice=ended.has_dice)


@dataclass(frozen=True)
class SessionEnded:
    conversation_id: int
    user_ids: tuple[int, int]
    reason: str
    has_dice: bool


async def end_session(
    session: AsyncSession,
    private_id: int,
    now: datetime,
    reason: str,
    *,
    ended_by: int | None = None,
) -> SessionEnded | None:
    """End an active session and queue its messages for deletion.

    Returns None if it had already ended: each session ends, and is queued, once.
    """
    conversation_id = await session.scalar(
        update(PrivateSession)
        .where(PrivateSession.id == private_id, PrivateSession.ended_at.is_(None))
        .values(ended_at=now, end_reason=reason, ended_by_user_id=ended_by)
        .returning(PrivateSession.conversation_id)
    )
    if conversation_id is None:
        return None
    await queue_deletions(session, now, ChatMessage.private_session_id == private_id)
    has_dice = await session.scalar(
        select(
            exists().where(ChatMessage.private_session_id == private_id, ChatMessage.kind == "dice")
        )
    )
    users = (
        await session.execute(
            select(ConversationParticipant.user_id)
            .where(ConversationParticipant.conversation_id == conversation_id)
            .order_by(ConversationParticipant.user_id)
        )
    ).scalars()
    first, second = users
    return SessionEnded(conversation_id, (first, second), reason, bool(has_dice))


async def queue_deletions(session: AsyncSession, now: datetime, which: ColumnElement[bool]) -> None:
    """Queue the sender's original and the partner's copy of the messages matching
    `which` (a condition on ChatMessage) for deletion from Telegram. For view-once
    media the partner's copy is the placeholder; media already opened is left out, as
    opening queued it for the end of its viewing time.

    A message already queued is not queued twice, and one already past the 48-hour
    limit is not queued at all.
    """
    expires = ChatMessage.created_at + DELETE_WITHIN
    not_before = case(
        (
            ChatMessage.kind == "dice",
            func.greatest(
                literal(now), ChatMessage.created_at + DICE_DELETABLE_AFTER + DICE_MARGIN
            ),
        ),
        else_=literal(now),
    )
    # Opened view-once media keeps its viewing time: opening already queued it (D-048).
    unopened = ChatMessage.viewed_at.is_(None)
    sender = aliased(User)
    recipient = aliased(User)
    partner = aliased(ConversationParticipant)
    originals = (
        select(sender.telegram_user_id, ChatMessage.sender_message_id, not_before, expires)
        .join(sender, sender.id == ChatMessage.sender_user_id)
        .where(which, unopened, expires > now)
    )
    copies = (
        select(recipient.telegram_user_id, ChatMessage.recipient_message_id, not_before, expires)
        .join(
            partner,
            and_(
                partner.conversation_id == ChatMessage.conversation_id,
                partner.user_id != ChatMessage.sender_user_id,
            ),
        )
        .join(recipient, recipient.id == partner.user_id)
        .where(which, unopened, ChatMessage.recipient_message_id.is_not(None), expires > now)
    )
    columns = ["chat_id", "message_id", "not_before", "expires_at"]
    for rows in (originals, copies):
        await session.execute(
            insert(TelegramDeletion)
            .from_select(columns, rows)
            .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
        )


async def session_ended(session: AsyncSession, private_id: int) -> bool:
    ended = await session.scalar(
        select(PrivateSession.ended_at).where(PrivateSession.id == private_id)
    )
    return ended is not None


async def sessions_to_end(
    session: AsyncSession, now: datetime, limit_after: timedelta, batch: int
) -> list[tuple[int, int]]:
    """Active sessions that look due to end (their chat has ended, or they reached the
    time limit), as (session id, conversation id). Nothing is locked: `end_if_due`
    decides under the conversation lock."""
    rows = await session.execute(
        select(PrivateSession.id, PrivateSession.conversation_id)
        .join(Conversation, Conversation.id == PrivateSession.conversation_id)
        .where(
            PrivateSession.ended_at.is_(None),
            or_(
                Conversation.ended_at.is_not(None),
                PrivateSession.started_at <= now - limit_after,
            ),
        )
        .order_by(PrivateSession.started_at)
        .limit(batch)
    )
    return [(int(private_id), int(conversation_id)) for private_id, conversation_id in rows]


async def end_if_due(
    session: AsyncSession,
    private_id: int,
    conversation_id: int,
    now: datetime,
    limit_after: timedelta,
) -> SessionEnded | None:
    """End a session if its chat has ended or it reached the time limit.

    The conversation row is locked first, as turning Private mode off, recording a
    message, linking a copy, and ending the chat all do. So a message being recorded or
    linked is either seen here and queued, or sees the session ended and queues itself;
    and a chat that is ending is waited for, so the reason is never a stale "time_limit".
    """
    chat_ended_at = (
        await session.execute(
            select(Conversation.ended_at)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one()
    started_at = await session.scalar(
        select(PrivateSession.started_at).where(
            PrivateSession.id == private_id, PrivateSession.ended_at.is_(None)
        )
    )
    if started_at is None:
        return None  # ended meanwhile
    if chat_ended_at is not None:
        reason = "chat_ended"
    elif started_at <= now - limit_after:
        reason = "time_limit"
    else:
        return None
    return await end_session(session, private_id, now, reason)


@dataclass(frozen=True)
class EndedBySomeoneElse:
    reason: str  # "turned_off" (by the partner) or "time_limit"
    session_id: int


async def ended_by_someone_else(
    session: AsyncSession, conversation_id: int, user_id: int, since: datetime
) -> EndedBySomeoneElse | None:
    """How Private mode ended, if it did at or after `since` without this user choosing
    it. None otherwise, including when it is on again, or this user turned it off
    themselves (D-049). Callers hold the conversation lock, so the answer stays true."""
    if await active_session(session, conversation_id) is not None:
        return None
    latest = (
        await session.execute(
            select(PrivateSession.end_reason, PrivateSession.ended_by_user_id, PrivateSession.id)
            .where(
                PrivateSession.conversation_id == conversation_id,
                PrivateSession.ended_at.is_not(None),
                PrivateSession.ended_at >= since,
            )
            .order_by(PrivateSession.ended_at.desc())
            .limit(1)
        )
    ).first()
    if latest is None:
        return None
    reason, ended_by, session_id = latest
    if reason == "time_limit" or (reason == "turned_off" and ended_by != user_id):
        return EndedBySomeoneElse(str(reason), int(session_id))
    return None


async def latest_session(session: AsyncSession, conversation_id: int) -> int | None:
    """The id of the conversation's most recent Private mode session, active or not."""
    return await session.scalar(
        select(PrivateSession.id)
        .where(PrivateSession.conversation_id == conversation_id)
        .order_by(PrivateSession.id.desc())
        .limit(1)
    )


# --- View-once media (D-048) ----------------------------------------------------------


class ViewRefused(StrEnum):
    NOT_YOURS = "not_yours"  # not the recipient of this message
    ALREADY_VIEWED = "already_viewed"
    UNAVAILABLE = "unavailable"  # Private mode or the chat has ended


@dataclass(frozen=True)
class Opening:
    """A view-once message the viewer may open now."""

    row_id: int
    conversation_id: int
    sender_chat_id: int
    sender_message_id: int
    viewer_chat_id: int
    placeholder_id: int
    view_seconds: int
    delete_at: datetime  # when the copy, placeholder, and original are deleted


async def open_view_once(
    session: AsyncSession, row_id: int, viewer_id: int, now: datetime
) -> Opening | ViewRefused:
    """Mark a view-once message as opened by its recipient, once, and queue the
    placeholder and the sender's original for the end of the viewing time.

    The conversation row is locked first, as ending Private mode does, so an opening and
    the end of the session never interleave: after the end, nothing can be opened, and
    media opened before it keeps its full viewing time (the end skips it). Queueing here,
    in the same transaction, means the original and placeholder are deleted even if the
    bot stops before the copy is sent.
    """
    conversation_id = await session.scalar(
        select(ChatMessage.conversation_id).where(ChatMessage.id == row_id, ChatMessage.view_once)
    )
    if conversation_id is None:
        return ViewRefused.NOT_YOURS
    chat_open = await _lock_open_conversation(session, conversation_id)
    message = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.id == row_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    is_participant = await session.scalar(
        select(
            exists().where(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == viewer_id,
            )
        )
    )
    if not is_participant or message.sender_user_id == viewer_id:
        return ViewRefused.NOT_YOURS
    if message.viewed_at is not None:
        return ViewRefused.ALREADY_VIEWED
    if (
        not chat_open
        or message.recipient_message_id is None
        or message.private_session_id is None
        or await session_ended(session, message.private_session_id)
    ):
        return ViewRefused.UNAVAILABLE
    message.viewed_at = now
    view_seconds = message.view_seconds or 0
    opening = Opening(
        row_id=message.id,
        conversation_id=conversation_id,
        sender_chat_id=await _telegram_id(session, message.sender_user_id),
        sender_message_id=message.sender_message_id,
        viewer_chat_id=await _telegram_id(session, viewer_id),
        placeholder_id=int(message.recipient_message_id),
        view_seconds=view_seconds,
        delete_at=now + timedelta(seconds=view_seconds),
    )
    expires = message.created_at + DELETE_WITHIN
    await _queue_at(session, opening.viewer_chat_id, opening.placeholder_id, opening, expires)
    await _queue_at(session, opening.sender_chat_id, opening.sender_message_id, opening, expires)
    return opening


async def _telegram_id(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(select(User.telegram_user_id).where(User.id == user_id))
        ).scalar_one()
    )


async def _queue_at(
    session: AsyncSession, chat_id: int, message_id: int, opening: Opening, expires: datetime
) -> None:
    """Queue one message for deletion at the end of the viewing time."""
    if opening.delete_at >= expires:
        return  # Telegram would refuse by then
    await session.execute(
        insert(TelegramDeletion)
        .values(
            chat_id=chat_id,
            message_id=message_id,
            not_before=opening.delete_at,
            expires_at=expires,
        )
        .on_conflict_do_update(
            index_elements=["chat_id", "message_id"], set_={"not_before": opening.delete_at}
        )
    )


async def opened(session: AsyncSession, opening: Opening, copy_id: int, now: datetime) -> None:
    """Record the copy sent to the viewer and queue it for the end of the viewing time."""
    await session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == opening.row_id)
        .values(viewed_message_id=copy_id)
    )
    await _queue_at(session, opening.viewer_chat_id, copy_id, opening, now + DELETE_WITHIN)


async def reopenable(session: AsyncSession, opening: Opening) -> bool:
    """Undo an opening whose copy Telegram asked the bot to delay, so it can be tapped
    again; returns whether it can.

    Only while Private mode and the chat are still on: otherwise the placeholder and
    original stay queued for the end of the viewing time, as the end skipped them.
    """
    if not await _lock_open_conversation(session, opening.conversation_id):
        return False
    private_id = await session.scalar(
        select(ChatMessage.private_session_id).where(ChatMessage.id == opening.row_id)
    )
    if private_id is None or await session_ended(session, private_id):
        return False
    await session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == opening.row_id, ChatMessage.viewed_message_id.is_(None))
        .values(viewed_at=None)
    )
    await session.execute(
        delete(TelegramDeletion).where(
            or_(
                and_(
                    TelegramDeletion.chat_id == opening.viewer_chat_id,
                    TelegramDeletion.message_id == opening.placeholder_id,
                ),
                and_(
                    TelegramDeletion.chat_id == opening.sender_chat_id,
                    TelegramDeletion.message_id == opening.sender_message_id,
                ),
            )
        )
    )
    return True


# --- The deletion queue ----------------------------------------------------------------


@dataclass(frozen=True)
class Due:
    id: int
    chat_id: int
    message_id: int


async def drop_expired(session: AsyncSession, now: datetime) -> int:
    """Remove entries past the 48-hour limit, which Telegram would refuse; returns how many."""
    result = await session.execute(
        delete(TelegramDeletion)
        .where(TelegramDeletion.expires_at <= now)
        .returning(TelegramDeletion.id)
    )
    return len(result.all())


async def claim_due(session: AsyncSession, now: datetime, limit: int) -> list[Due]:
    """Claim up to `limit` due entries for one worker to try.

    Claimed entries are pushed back by a short lease, so another worker leaves them
    alone, and an attempt cut short by a crash is retried once the lease runs out.
    """
    due = (
        select(TelegramDeletion.id)
        .where(TelegramDeletion.not_before <= now, TelegramDeletion.expires_at > now)
        .order_by(TelegramDeletion.not_before)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    lease_until = func.least(
        literal(now + CLAIM_LEASE), TelegramDeletion.expires_at - timedelta(seconds=1)
    )
    rows = await session.execute(
        update(TelegramDeletion)
        .where(TelegramDeletion.id.in_(due.scalar_subquery()))
        .values(not_before=lease_until)
        .returning(TelegramDeletion.id, TelegramDeletion.chat_id, TelegramDeletion.message_id)
    )
    return [Due(int(i), int(c), int(m)) for i, c, m in rows]


async def defer(session: AsyncSession, ids: list[int], until: datetime) -> None:
    """Hold entries back until `until` (but never past their deadline), e.g. when
    Telegram asks the bot to slow down."""
    if ids:
        await session.execute(
            update(TelegramDeletion)
            .where(TelegramDeletion.id.in_(ids))
            .values(
                not_before=func.least(
                    literal(until), TelegramDeletion.expires_at - timedelta(seconds=1)
                )
            )
        )


async def remove(session: AsyncSession, ids: list[int]) -> None:
    if ids:
        await session.execute(delete(TelegramDeletion).where(TelegramDeletion.id.in_(ids)))


# --- The worker job --------------------------------------------------------------------

BATCH_SIZE = 100


class PrivateNotifier(Protocol):
    """Tells both users that Private mode ended. Implemented by the bot layer."""

    async def private_ended(self, ended: SessionEnded) -> None: ...


class PrivateModeJob:
    """Ends Private mode when its chat has ended or its time limit is reached. Runs in
    the worker, in a loop of its own (its notices call Telegram); the deletions it
    queues are sent by the bot layer."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
        notifier: PrivateNotifier,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime_settings
        self._notifier = notifier

    async def tick(self, now: datetime) -> int:
        """End the sessions that are due; returns how many ended."""
        async with self._sessions() as session:
            limit_after = timedelta(
                hours=await self._runtime.get(session, SettingKey.PRIVATE_MODE_HOURS)
            )
            candidates = await sessions_to_end(session, now, limit_after, BATCH_SIZE)
            await session.commit()
        ended = []
        for private_id, conversation_id in candidates:
            # One short transaction each, so no conversation stays locked for long.
            async with self._sessions() as session:
                done = await end_if_due(session, private_id, conversation_id, now, limit_after)
                await session.commit()
            if done is not None:
                ended.append(done)
        for done in ended:
            try:
                await self._notifier.private_ended(done)
            except Exception as exc:
                logger.warning(
                    "could not tell users Private mode ended", extra=exception_summary(exc)
                )
        return len(ended)
