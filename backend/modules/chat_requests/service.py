"""Chat requests (D-058).

Like the other services, these take an AsyncSession and leave committing to the
caller, and user ids are internal `users.id` values. A request moves through these
states, each step a conditional update, so a repeated or concurrent step happens once:

* pending -> delivered (`delivered`: the notice reached the recipient and the price is
  taken) or failed (`failed`, or `fail_stale` after an interruption: the hold is
  released). A delivered request is never refunded.
* delivered -> declined (`decline`), expired (`expire_due`), closed (the sender
  connected through another request), connected (`connect`), or accepted
  (`mark_accepted`: the recipient accepted while the sender was busy).
* accepted -> connected (the sender switched), lapsed (`stay`, or `lapse_due` when the
  sender doesn't answer in time), or closed.

`connect` ends any chat either user is in and creates their conversation in one
transaction, under the same unique index on open chats as matching, so a request
creates at most one conversation and never double-books anyone. It locks the account
rows before the request row, the order account deletion uses, so the two never
deadlock.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.chat_requests.models import WAITING_STATUSES, ChatRequest
from backend.modules.coins import service as coins
from backend.modules.coins.service import InsufficientCoinsError
from backend.modules.moderation import service as moderation
from backend.modules.users import service as users
from backend.modules.users.models import Profile, TermsAcceptance, User
from backend.modules.users.service import Notification

logger = logging.getLogger(__name__)

PURPOSE = "chat_request"
# A request still pending after this long was interrupted: delivering one normally
# takes a second or two.
STALE_AFTER = timedelta(minutes=5)


class Refusal(StrEnum):
    # The recipient can't take a request now: inactive, chatting, in Silent mode, they
    # blocked the sender, or the account can't use RealChat. No reason is given.
    UNAVAILABLE = "unavailable"
    YOU_BLOCKED = "you_blocked"  # the sender blocked the recipient
    BUSY = "busy"  # the sender is chatting or searching
    WAITING = "waiting"  # the sender already has a request waiting for this person
    NO_COINS = "no_coins"


@dataclass(frozen=True)
class Sent:
    request_id: int | None  # None when refused
    refusal: Refusal | None = None
    already: bool = False  # this Send was already recorded: nothing new happened


@dataclass(frozen=True)
class Parties:
    request_id: int
    sender_id: int
    recipient_id: int


class ConnectOutcome(StrEnum):
    CONNECTED = "connected"
    GONE = "gone"  # no longer waiting to be connected (answered, expired, or closed)
    UNAVAILABLE = "unavailable"  # one of the two can no longer chat; the request closed
    RACED = "raced"  # one of them was matched meanwhile; roll back and tell them


@dataclass(frozen=True)
class Connected:
    outcome: ConnectOutcome
    conversation_id: int | None = None
    # Chats that were ended to connect, as (user who left, the chat they left).
    ended: list[tuple[int, chat.ActiveChat]] = field(default_factory=list)


async def _lock_ready(session: AsyncSession, first: int, second: int) -> bool:
    """Lock both accounts FOR KEY SHARE, in id order, and check both can use RealChat.
    Account deletion locks the account FOR UPDATE, so the two never overlap."""
    ready = await session.scalars(
        select(User.id)
        .join(Profile, Profile.user_id == User.id)
        .where(
            User.id.in_((first, second)),
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
        )
        .order_by(User.id)
        .with_for_update(key_share=True, of=User)
    )
    return len(ready.all()) == 2


async def _waiting(session: AsyncSession, sender_id: int, recipient_id: int) -> bool:
    found = await session.scalar(
        select(ChatRequest.id).where(
            ChatRequest.sender_user_id == sender_id,
            ChatRequest.recipient_user_id == recipient_id,
            ChatRequest.status.in_(WAITING_STATUSES),
        )
    )
    return found is not None


async def refusal(
    session: AsyncSession,
    sender_id: int,
    recipient_id: int,
    active_since: datetime,
    *,
    sender_searching: bool,
) -> Refusal | None:
    """Why `sender_id` can't send `recipient_id` a request now, or None if they can.

    The recipient must have used RealChat since `active_since`, not be chatting, and not
    be in Silent mode; the sender must not be chatting or searching (`sender_searching`,
    from the search queue); neither may have blocked the other. Locks both accounts.
    """
    if sender_id == recipient_id or not await _lock_ready(session, sender_id, recipient_id):
        return Refusal.UNAVAILABLE
    if await moderation.is_blocked(session, sender_id, recipient_id):
        return Refusal.YOU_BLOCKED
    if await moderation.is_blocked(session, recipient_id, sender_id):
        return Refusal.UNAVAILABLE
    if sender_searching or await chat.active_chat(session, sender_id) is not None:
        return Refusal.BUSY
    if await _waiting(session, sender_id, recipient_id):
        return Refusal.WAITING
    active = await session.scalar(
        select(User.id).where(User.id == recipient_id, User.last_active_at >= active_since)
    )
    if active is None or await chat.active_chat(session, recipient_id) is not None:
        return Refusal.UNAVAILABLE
    if (await users.notification_settings(session, recipient_id))[Notification.SILENT_MODE]:
        return Refusal.UNAVAILABLE
    return None


async def send(
    session: AsyncSession,
    sender_id: int,
    recipient_id: int,
    key: str,
    price: int,
    active_since: datetime,
    now: datetime,
    *,
    sender_searching: bool,
) -> Sent:
    """Record a request, pending until its notice is delivered, and hold its price.

    `key` names this Send: if it was already recorded, that request is returned with
    `already` set and nothing else happens.
    """
    existing = await session.scalar(
        select(ChatRequest.id).where(ChatRequest.idempotency_key == key)
    )
    if existing is not None:
        return Sent(int(existing), already=True)
    refused = await refusal(
        session, sender_id, recipient_id, active_since, sender_searching=sender_searching
    )
    if refused is not None:
        return Sent(None, refused)
    try:
        hold_id = await coins.hold(session, sender_id, price, PURPOSE, f"creq:{key}")
    except InsufficientCoinsError:
        return Sent(None, Refusal.NO_COINS)
    request_id = await session.scalar(
        insert(ChatRequest)
        .values(
            sender_user_id=sender_id,
            recipient_user_id=recipient_id,
            idempotency_key=key,
            price=price,
            hold_id=hold_id,
        )
        # The same Send, or another request to the same person, got there first.
        .on_conflict_do_nothing()
        .returning(ChatRequest.id)
    )
    if request_id is None:
        await coins.release(session, hold_id, now)
        same = await session.scalar(
            select(ChatRequest.id).where(ChatRequest.idempotency_key == key)
        )
        return Sent(int(same), already=True) if same is not None else Sent(None, Refusal.WAITING)
    return Sent(int(request_id))


async def sent_under(session: AsyncSession, key: str) -> bool:
    """Whether a request was already recorded for the Send named `key`."""
    found = await session.scalar(select(ChatRequest.id).where(ChatRequest.idempotency_key == key))
    return found is not None


async def delivered(session: AsyncSession, request_id: int, now: datetime) -> bool:
    """The notice was delivered: take the price. Returns whether this call did it.

    The sender's account is locked FOR KEY SHARE before the request, the order account
    deletion uses, so the two never deadlock.
    """
    sender_id = await session.scalar(
        select(ChatRequest.sender_user_id).where(ChatRequest.id == request_id)
    )
    if sender_id is None:
        return False
    await session.execute(
        select(User.id).where(User.id == sender_id).with_for_update(key_share=True)
    )
    hold_id = await session.scalar(
        update(ChatRequest)
        .where(ChatRequest.id == request_id, ChatRequest.status == "pending")
        .values(status="delivered", delivered_at=now)
        .returning(ChatRequest.hold_id)
    )
    if hold_id is None:
        return False
    await coins.capture(session, hold_id, now)
    return True


async def failed(session: AsyncSession, request_id: int, reason: str, now: datetime) -> bool:
    """The notice could not be delivered: release the hold and record why."""
    hold_id = await session.scalar(
        update(ChatRequest)
        .where(ChatRequest.id == request_id, ChatRequest.status == "pending")
        .values(status="failed", failure_reason=reason, settled_at=now)
        .returning(ChatRequest.hold_id)
    )
    if hold_id is None:
        return False
    await coins.release(session, hold_id, now)
    return True


async def fail_stale(session: AsyncSession, before: datetime, now: datetime) -> int:
    """Fail requests still pending since before `before`, releasing their holds. A
    safety net for a bot stopped between recording a request and settling it: the price
    is not taken, even if the notice did reach the recipient."""
    stale = (
        select(ChatRequest.id)
        .where(ChatRequest.status == "pending", ChatRequest.created_at < before)
        .with_for_update(skip_locked=True)
        .cte("stale")
        .prefix_with("MATERIALIZED")
    )
    holds = (
        await session.scalars(
            update(ChatRequest)
            .where(ChatRequest.status == "pending", ChatRequest.id.in_(select(stale.c.id)))
            .values(status="failed", failure_reason="interrupted", settled_at=now)
            .returning(ChatRequest.hold_id)
        )
    ).all()
    for hold_id in holds:
        await coins.release(session, hold_id, now)
    if holds:
        logger.warning("failed chat requests left pending", extra={"count": len(holds)})
    return len(holds)


async def answerable(
    session: AsyncSession, request_id: int, recipient_id: int, since: datetime
) -> Parties | None:
    """The request, if it was delivered to `recipient_id` at or after `since` (the
    expiry start) and is still waiting for an answer."""
    row = (
        await session.execute(
            select(ChatRequest.id, ChatRequest.sender_user_id, ChatRequest.recipient_user_id).where(
                ChatRequest.id == request_id,
                ChatRequest.recipient_user_id == recipient_id,
                ChatRequest.status == "delivered",
                ChatRequest.delivered_at >= since,
            )
        )
    ).first()
    return None if row is None else Parties(int(row[0]), int(row[1]), int(row[2]))


async def decline(
    session: AsyncSession, request_id: int, recipient_id: int, since: datetime, now: datetime
) -> Parties | None:
    """Decline a request still waiting for an answer; returns it if this call did."""
    row = (
        await session.execute(
            update(ChatRequest)
            .where(
                ChatRequest.id == request_id,
                ChatRequest.recipient_user_id == recipient_id,
                ChatRequest.status == "delivered",
                ChatRequest.delivered_at >= since,
            )
            .values(status="declined", settled_at=now)
            .returning(ChatRequest.id, ChatRequest.sender_user_id, ChatRequest.recipient_user_id)
        )
    ).first()
    return None if row is None else Parties(int(row[0]), int(row[1]), int(row[2]))


async def mark_accepted(
    session: AsyncSession, request_id: int, recipient_id: int, since: datetime, now: datetime
) -> Parties | None:
    """The recipient accepted while the sender is busy: the sender may now switch."""
    row = (
        await session.execute(
            update(ChatRequest)
            .where(
                ChatRequest.id == request_id,
                ChatRequest.recipient_user_id == recipient_id,
                ChatRequest.status == "delivered",
                ChatRequest.delivered_at >= since,
            )
            .values(status="accepted", accepted_at=now)
            .returning(ChatRequest.id, ChatRequest.sender_user_id, ChatRequest.recipient_user_id)
        )
    ).first()
    return None if row is None else Parties(int(row[0]), int(row[1]), int(row[2]))


async def switchable(
    session: AsyncSession, request_id: int, sender_id: int, since: datetime
) -> Parties | None:
    """The request, if it was accepted at or after `since` (the switch window start)
    and the sender (`sender_id`) hasn't answered yet."""
    row = (
        await session.execute(
            select(ChatRequest.id, ChatRequest.sender_user_id, ChatRequest.recipient_user_id).where(
                ChatRequest.id == request_id,
                ChatRequest.sender_user_id == sender_id,
                ChatRequest.status == "accepted",
                ChatRequest.accepted_at >= since,
            )
        )
    ).first()
    return None if row is None else Parties(int(row[0]), int(row[1]), int(row[2]))


async def stay(
    session: AsyncSession, request_id: int, sender_id: int, now: datetime
) -> Parties | None:
    """The busy sender chose to stay: the request lapses. Returns it if this call did."""
    row = (
        await session.execute(
            update(ChatRequest)
            .where(
                ChatRequest.id == request_id,
                ChatRequest.sender_user_id == sender_id,
                ChatRequest.status == "accepted",
            )
            .values(status="lapsed", settled_at=now)
            .returning(ChatRequest.id, ChatRequest.sender_user_id, ChatRequest.recipient_user_id)
        )
    ).first()
    return None if row is None else Parties(int(row[0]), int(row[1]), int(row[2]))


async def _settle_due(
    session: AsyncSession, status: str, column: str, before: datetime, to: str, now: datetime
) -> list[Parties]:
    due = (
        select(ChatRequest.id)
        .where(ChatRequest.status == status, getattr(ChatRequest, column) < before)
        .with_for_update(skip_locked=True)
        .cte("due")
        .prefix_with("MATERIALIZED")
    )
    rows = await session.execute(
        update(ChatRequest)
        .where(ChatRequest.status == status, ChatRequest.id.in_(select(due.c.id)))
        .values(status=to, settled_at=now)
        .returning(ChatRequest.id, ChatRequest.sender_user_id, ChatRequest.recipient_user_id)
    )
    return [Parties(int(a), int(b), int(c)) for a, b, c in rows]


async def expire_due(session: AsyncSession, before: datetime, now: datetime) -> list[Parties]:
    """Expire requests delivered before `before` and never answered."""
    return await _settle_due(session, "delivered", "delivered_at", before, "expired", now)


async def lapse_due(session: AsyncSession, before: datetime, now: datetime) -> list[Parties]:
    """Lapse requests whose busy sender didn't answer since before `before`."""
    return await _settle_due(session, "accepted", "accepted_at", before, "lapsed", now)


async def _lock_chattable(
    session: AsyncSession, first: int, second: int, terms_version: str
) -> bool:
    """Share-lock both accounts (in id order) and check both can chat: active,
    onboarded, with the current terms accepted. Whether they are in a chat is not
    checked here: `connect` ends those chats."""
    accepted = exists().where(
        TermsAcceptance.user_id == User.id, TermsAcceptance.terms_version == terms_version
    )
    ready = await session.scalars(
        select(User.id)
        .join(Profile, Profile.user_id == User.id)
        .where(
            User.id.in_((first, second)),
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
            accepted,
        )
        .order_by(User.id)
        .with_for_update(read=True, of=User)
    )
    return len(ready.all()) == 2


async def connect(
    session: AsyncSession,
    request_id: int,
    from_status: str,
    since: datetime,
    now: datetime,
    terms_version: str,
) -> Connected:
    """Connect the two users of a request that is `from_status` ("delivered" since
    `since`, or "accepted" since `since`), ending any chat either is in.

    On RACED the caller must roll back: a chat may already have been ended.
    """
    parties = (
        await session.execute(
            select(ChatRequest.sender_user_id, ChatRequest.recipient_user_id).where(
                ChatRequest.id == request_id
            )
        )
    ).first()
    if parties is None:
        return Connected(ConnectOutcome.GONE)
    sender_id, recipient_id = int(parties[0]), int(parties[1])
    # Accounts first, then the request: the order account deletion uses.
    chattable = await _lock_chattable(session, sender_id, recipient_id, terms_version)
    timed = ChatRequest.delivered_at if from_status == "delivered" else ChatRequest.accepted_at
    locked = await session.scalar(
        select(ChatRequest.id)
        .where(ChatRequest.id == request_id, ChatRequest.status == from_status, timed >= since)
        .with_for_update()
    )
    if locked is None:
        return Connected(ConnectOutcome.GONE)
    if (
        not chattable
        or await moderation.is_blocked(session, sender_id, recipient_id)
        or (await moderation.is_blocked(session, recipient_id, sender_id))
    ):
        await session.execute(
            update(ChatRequest)
            .where(ChatRequest.id == request_id)
            .values(status="closed", settled_at=now)
        )
        return Connected(ConnectOutcome.UNAVAILABLE)
    together = await chat.active_chat(session, sender_id)
    if together is not None and together.partner_id == recipient_id:
        # Matched with each other meanwhile: that chat is the one they asked for.
        await _mark_connected(session, request_id, sender_id, together.conversation_id, now)
        return Connected(ConnectOutcome.CONNECTED, together.conversation_id)
    ended: list[tuple[int, chat.ActiveChat]] = []
    for user_id in (recipient_id, sender_id):
        left = await chat.end_conversation(session, user_id, now)
        if left is not None:
            ended.append((user_id, left))
    try:
        async with session.begin_nested():
            conversation_id = await chat.start_conversation(session, sender_id, recipient_id)
    except IntegrityError:
        return Connected(ConnectOutcome.RACED)
    await _mark_connected(session, request_id, sender_id, conversation_id, now)
    return Connected(ConnectOutcome.CONNECTED, conversation_id, ended)


async def _mark_connected(
    session: AsyncSession, request_id: int, sender_id: int, conversation_id: int, now: datetime
) -> None:
    await session.execute(
        update(ChatRequest)
        .where(ChatRequest.id == request_id)
        .values(status="connected", conversation_id=conversation_id, settled_at=now)
    )
    # The sender's other requests can't be taken up any more.
    await session.execute(
        update(ChatRequest)
        .where(
            ChatRequest.sender_user_id == sender_id,
            ChatRequest.id != request_id,
            ChatRequest.status.in_(("delivered", "accepted")),
        )
        .values(status="closed", settled_at=now)
    )


# --- Worker ----------------------------------------------------------------------------


class RequestNotifier(Protocol):
    async def expired(self, parties: Parties) -> None: ...

    async def lapsed(self, parties: Parties) -> None: ...


class RequestJobs:
    """Settles requests nobody answered in time, and sends left pending by an
    interruption, then tells the users (best effort, after committing)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
        notifier: RequestNotifier,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime_settings
        self._notifier = notifier

    async def tick(self, now: datetime) -> int:
        """Returns how many requests were settled."""
        async with self._sessions() as session:
            expiry = await self._runtime.get(session, SettingKey.CHAT_REQUEST_EXPIRY_MINUTES)
            window = await self._runtime.get(session, SettingKey.CHAT_REQUEST_SWITCH_SECONDS)
            failed_now = await fail_stale(session, now - STALE_AFTER, now)
            expired = await expire_due(session, now - timedelta(minutes=expiry), now)
            lapsed = await lapse_due(session, now - timedelta(seconds=window), now)
            await session.commit()
        for parties in expired:
            await self._notifier.expired(parties)
        for parties in lapsed:
            await self._notifier.lapsed(parties)
        return failed_now + len(expired) + len(lapsed)
