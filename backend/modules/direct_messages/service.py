"""Direct messages and free replies (D-057).

Like the other services, these take an AsyncSession and leave committing to the
caller, and user ids are internal `users.id` values. A message moves through
pending -> delivered -> viewed, or pending -> failed, each step a conditional update,
so a repeated or concurrent step happens once:

* `send` checks both users, holds the price (nothing for a free reply), and records
  the message as pending. The caller commits, then sends the recipient's notice.
* `delivered` takes the held coin once Telegram has accepted the notice; `failed`
  releases it and records why. If the bot stops in between, the worker's
  `fail_stale` fails the message later and releases the hold, so a coin is only ever
  taken for a notice Telegram confirmed.
* `mark_viewed` records the first opening, so the sender is told once.

Nobody can message, or freely reply to, someone who blocked them or whom they blocked.
A paid message allows the recipient one free reply (a unique index enforces it); a
free reply allows none. Messages are kept for the chat message retention period (an
admin setting), counted from sending, and can't be opened or replied to after it.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.chat import service as chat
from backend.modules.coins import service as coins
from backend.modules.coins.service import InsufficientCoinsError
from backend.modules.direct_messages.models import DirectMessage
from backend.modules.moderation import service as moderation
from backend.modules.users.models import Profile, User

logger = logging.getLogger(__name__)

PURPOSE = "direct_message"


class Refusal(StrEnum):
    UNAVAILABLE = "unavailable"  # either account can't use RealChat, or a self-message
    BLOCKED = "blocked"  # the recipient blocked the sender
    YOU_BLOCKED = "you_blocked"  # the sender blocked the recipient
    IN_CHAT = "in_chat"  # the two are chatting right now
    NO_COINS = "no_coins"
    EXPIRED = "expired"  # the message being replied to is gone or can't be replied to
    REPLIED = "replied"  # its free reply was already sent


@dataclass(frozen=True)
class Content:
    kind: str
    text: str | None
    entities: list[dict[str, Any]] | None
    file_id: str | None


@dataclass(frozen=True)
class Sent:
    message_id: int | None  # None when refused
    refusal: Refusal | None = None
    already: bool = False  # this Send was already recorded: nothing new happened


async def _lock_ready(session: AsyncSession, first: int, second: int) -> bool:
    """Lock both accounts (FOR NO KEY UPDATE, in id order, until the transaction ends)
    and check both can use RealChat.

    Account deletion locks the account FOR UPDATE, so a message and a deletion never
    overlap: one waiting on a deletion sees the account gone and is refused.
    """
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


async def refusal(session: AsyncSession, sender_id: int, recipient_id: int) -> Refusal | None:
    """Why `sender_id` can't message `recipient_id` now, or None if they can. Locks
    both accounts (see `_lock_ready`)."""
    if sender_id == recipient_id or not await _lock_ready(session, sender_id, recipient_id):
        return Refusal.UNAVAILABLE
    if await moderation.is_blocked(session, recipient_id, sender_id):
        return Refusal.BLOCKED
    if await moderation.is_blocked(session, sender_id, recipient_id):
        return Refusal.YOU_BLOCKED
    active = await chat.active_chat(session, sender_id)
    if active is not None and active.partner_id == recipient_id:
        return Refusal.IN_CHAT
    return None


def retention_start(now: datetime, days: int) -> datetime:
    """The earliest sending time of a message that can still be opened."""
    return now - timedelta(days=days)


async def _repliable(
    session: AsyncSession, message_id: int, user_id: int, since: datetime
) -> DirectMessage | None:
    """The paid message `message_id` sent to `user_id`, if it was delivered within the
    retention period, so it can still get its free reply."""
    return await session.scalar(
        select(DirectMessage).where(
            DirectMessage.id == message_id,
            DirectMessage.recipient_user_id == user_id,
            DirectMessage.price > 0,
            DirectMessage.status.in_(("delivered", "viewed")),
            DirectMessage.created_at >= since,
        )
    )


async def reply_refusal(
    session: AsyncSession, message_id: int, user_id: int, since: datetime
) -> tuple[int | None, Refusal | None]:
    """Who a free reply to `message_id` would go to, and why it can't be sent, if it
    can't. Locks both accounts."""
    original = await _repliable(session, message_id, user_id, since)
    if original is None:
        return None, Refusal.EXPIRED
    sender_id = original.sender_user_id
    refused = await refusal(session, user_id, sender_id)
    if refused is not None:
        return sender_id, refused
    replied = await session.scalar(
        select(DirectMessage.id).where(DirectMessage.reply_to_id == message_id)
    )
    return sender_id, None if replied is None else Refusal.REPLIED


async def send(
    session: AsyncSession,
    sender_id: int,
    recipient_id: int,
    content: Content,
    key: str,
    price: int,
    *,
    reply_to: int | None = None,
    since: datetime | None = None,
) -> Sent:
    """Record a message from `sender_id`, pending until its notice is delivered.

    `key` names this Send: if it was already recorded, that message is returned with
    `already` set and nothing else happens. A paid message holds `price` coins; a free
    reply (`reply_to`, with `since` the retention start) costs nothing and is refused
    if the message it answers already has one.
    """
    existing = await session.scalar(
        select(DirectMessage.id).where(DirectMessage.idempotency_key == key)
    )
    if existing is not None:
        return Sent(int(existing), already=True)
    if reply_to is not None:
        if since is None:
            raise ValueError("a free reply needs the retention start")
        replied_to, refused = await reply_refusal(session, reply_to, sender_id, since)
        if refused is not None or replied_to != recipient_id:
            return Sent(None, refused or Refusal.EXPIRED)
        price = 0
    else:
        refused = await refusal(session, sender_id, recipient_id)
        if refused is not None:
            return Sent(None, refused)
    hold_id = None
    if price > 0:
        try:
            hold_id = await coins.hold(session, sender_id, price, PURPOSE, f"dm:{key}")
        except InsufficientCoinsError:
            return Sent(None, Refusal.NO_COINS)
    message_id = await session.scalar(
        insert(DirectMessage)
        .values(
            sender_user_id=sender_id,
            recipient_user_id=recipient_id,
            reply_to_id=reply_to,
            idempotency_key=key,
            kind=content.kind,
            text=content.text,
            entities=content.entities,
            file_id=content.file_id,
            price=price,
            hold_id=hold_id,
        )
        # A concurrent free reply to the same message, or the same Send, got there first.
        .on_conflict_do_nothing()
        .returning(DirectMessage.id)
    )
    if message_id is None:
        same = await session.scalar(
            select(DirectMessage.id).where(DirectMessage.idempotency_key == key)
        )
        return Sent(int(same), already=True) if same is not None else Sent(None, Refusal.REPLIED)
    return Sent(int(message_id))


async def sent_under(session: AsyncSession, key: str) -> bool:
    """Whether a message was already recorded for the Send named `key`."""
    found = await session.scalar(
        select(DirectMessage.id).where(DirectMessage.idempotency_key == key)
    )
    return found is not None


async def delivered(session: AsyncSession, message_id: int, now: datetime) -> bool:
    """The notice was delivered: take the held coin. Returns whether this call did it
    (False if the message is no longer pending, e.g. the worker failed it meanwhile,
    or it was deleted with an account).

    The sender's account row is locked (FOR NO KEY UPDATE) before the message, the order
    account deletion uses (account, then messages), so the two never deadlock: a
    delivery waiting on the sender's deletion then finds the message gone.
    """
    sender_id = await session.scalar(
        select(DirectMessage.sender_user_id).where(DirectMessage.id == message_id)
    )
    if sender_id is None:
        return False
    await session.execute(
        select(User.id).where(User.id == sender_id).with_for_update(key_share=True)
    )
    row = (
        await session.execute(
            update(DirectMessage)
            .where(DirectMessage.id == message_id, DirectMessage.status == "pending")
            .values(status="delivered", delivered_at=now)
            .returning(DirectMessage.hold_id)
        )
    ).first()
    if row is None:
        return False
    if row[0] is not None:
        await coins.capture(session, row[0], now)
    return True


async def failed(session: AsyncSession, message_id: int, reason: str, now: datetime) -> bool:
    """The notice could not be delivered: release the hold and record why. Returns
    whether this call failed it."""
    row = (
        await session.execute(
            update(DirectMessage)
            .where(DirectMessage.id == message_id, DirectMessage.status == "pending")
            .values(status="failed", failure_reason=reason)
            .returning(DirectMessage.hold_id)
        )
    ).first()
    if row is None:
        return False
    if row[0] is not None:
        await coins.release(session, row[0], now)
    return True


async def fail_stale(session: AsyncSession, before: datetime, now: datetime) -> int:
    """Fail messages still pending since before `before`, releasing their holds; returns
    how many. A safety net for a bot stopped between recording a message and settling
    it: the coin is not taken, even if the notice did reach the recipient."""
    stale = (
        select(DirectMessage.id)
        .where(DirectMessage.status == "pending", DirectMessage.created_at < before)
        # A message being delivered right now is left for the next run.
        .with_for_update(skip_locked=True)
        .cte("stale")
        .prefix_with("MATERIALIZED")
    )
    rows = await session.execute(
        update(DirectMessage)
        .where(
            DirectMessage.status == "pending",
            DirectMessage.id.in_(select(stale.c.id)),
        )
        .values(status="failed", failure_reason="interrupted")
        .returning(DirectMessage.hold_id)
    )
    holds = [hold for (hold,) in rows]
    for hold in holds:
        if hold is not None:
            await coins.release(session, hold, now)
    if holds:
        logger.warning("failed direct messages left pending", extra={"count": len(holds)})
    return len(holds)


async def for_recipient(
    session: AsyncSession, message_id: int, user_id: int, since: datetime
) -> DirectMessage | None:
    """The delivered message `message_id`, if it was sent to `user_id` within the
    retention period; None otherwise, so an old or forged button opens nothing."""
    return await session.scalar(
        select(DirectMessage)
        .where(
            DirectMessage.id == message_id,
            DirectMessage.recipient_user_id == user_id,
            DirectMessage.status.in_(("delivered", "viewed")),
            DirectMessage.created_at >= since,
        )
        .execution_options(populate_existing=True)
    )


async def mark_viewed(session: AsyncSession, message_id: int, now: datetime) -> bool:
    """Record the first opening; returns whether this call recorded it."""
    viewed = await session.scalar(
        update(DirectMessage)
        .where(DirectMessage.id == message_id, DirectMessage.status == "delivered")
        .values(status="viewed", viewed_at=now)
        .returning(DirectMessage.id)
    )
    return viewed is not None


async def delete_expired(session: AsyncSession, cutoff: datetime, limit: int) -> int:
    """Delete up to `limit` messages sent before `cutoff`; returns how many."""
    expired = (
        select(DirectMessage.id)
        .where(DirectMessage.created_at < cutoff, DirectMessage.status != "pending")
        .limit(limit)
        .with_for_update(skip_locked=True)
        # Materialized, so the batch is chosen once (as for visits).
        .cte("expired")
        .prefix_with("MATERIALIZED")
    )
    result = await session.execute(
        delete(DirectMessage)
        .where(DirectMessage.id.in_(select(expired.c.id)))
        .returning(DirectMessage.id)
    )
    return len(result.all())


# --- Worker ----------------------------------------------------------------------------

# A message still pending after this long was interrupted: delivering one normally
# takes a second or two.
STALE_AFTER = timedelta(minutes=5)


class PendingSweep:
    """Fails messages left pending by an interrupted Send, releasing their holds."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def tick(self, now: datetime) -> int:
        async with self._sessions() as session:
            failed_now = await fail_stale(session, now - STALE_AFTER, now)
            await session.commit()
        return failed_now
