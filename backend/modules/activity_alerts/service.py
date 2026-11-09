"""Activity alerts: one paid alert the next time someone uses RealChat (D-059).

Like the other services, these take an AsyncSession and leave committing to the
caller, and user ids are internal `users.id` values. This is RealChat activity
(`users.last_active_at`, recorded at most once a minute), never Telegram presence.

The price is taken when the alert is bought. The alert then ends exactly once, by a
conditional update from `active`: it fires (the person used RealChat after the
purchase), is refunded (no activity within the period, a block either way, the person
turned activity alerts off, or their account can no longer use RealChat), or is
cancelled by the buyer, without a refund. The worker fires and refunds; each refund is
keyed to its alert, so it is credited once even if the worker runs again.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import ColumnElement, and_, exists, false, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from backend.core.logging import exception_summary
from backend.modules.activity_alerts.models import ActivityAlert
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.coins import service as coins
from backend.modules.coins.service import InsufficientCoinsError
from backend.modules.moderation import service as moderation
from backend.modules.moderation.models import UserBlock
from backend.modules.users import service as users
from backend.modules.users.models import NotificationSettings, Profile, User
from backend.modules.users.service import Notification

logger = logging.getLogger(__name__)

KIND = "activity_alert"
REFUND_KIND = "activity_alert_refund"


class Refusal(StrEnum):
    UNAVAILABLE = "unavailable"  # either account can't use RealChat, or a self-alert
    BLOCKED = "blocked"  # the person blocked the buyer
    YOU_BLOCKED = "you_blocked"  # the buyer blocked the person
    OPTED_OUT = "opted_out"  # the person doesn't allow activity alerts
    PENDING = "pending"  # the buyer already has an alert set for this person
    NO_COINS = "no_coins"


@dataclass(frozen=True)
class Bought:
    alert_id: int | None  # None when refused
    refusal: Refusal | None = None
    already: bool = False  # this Buy was already recorded: nothing new happened


@dataclass(frozen=True)
class Settled:
    alert_id: int
    buyer_id: int
    target_id: int
    price: int
    reason: str | None = None  # the refund reason; None when it fired


async def _ready(session: AsyncSession, user_id: int) -> bool:
    found = await session.scalar(
        select(User.id)
        .join(Profile, Profile.user_id == User.id)
        .where(
            User.id == user_id,
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
        )
    )
    return found is not None


async def pending(session: AsyncSession, buyer_id: int, target_id: int) -> int | None:
    """The buyer's active alert for this person, if any."""
    return await session.scalar(
        select(ActivityAlert.id).where(
            ActivityAlert.buyer_user_id == buyer_id,
            ActivityAlert.target_user_id == target_id,
            ActivityAlert.status == "active",
        )
    )


async def refusal(session: AsyncSession, buyer_id: int, target_id: int) -> Refusal | None:
    """Why `buyer_id` can't buy an alert for `target_id` now, or None if they can."""
    if buyer_id == target_id or not (
        await _ready(session, buyer_id) and await _ready(session, target_id)
    ):
        return Refusal.UNAVAILABLE
    if await moderation.is_blocked(session, buyer_id, target_id):
        return Refusal.YOU_BLOCKED
    if await moderation.is_blocked(session, target_id, buyer_id):
        return Refusal.BLOCKED
    if not (await users.notification_settings(session, target_id))[Notification.ACTIVITY_ALERTS]:
        return Refusal.OPTED_OUT
    if await pending(session, buyer_id, target_id) is not None:
        return Refusal.PENDING
    return None


async def buy(
    session: AsyncSession, buyer_id: int, target_id: int, key: str, price: int, now: datetime
) -> Bought:
    """Buy an alert for `target_id`, taking `price` coins now.

    `key` names this Buy: if it was already recorded, that alert is returned with
    `already` set and nothing else happens. The alert and its charge are written
    together, so neither exists without the other.
    """
    existing = await session.scalar(
        select(ActivityAlert.id).where(ActivityAlert.idempotency_key == key)
    )
    if existing is not None:
        return Bought(int(existing), already=True)
    refused = await refusal(session, buyer_id, target_id)
    if refused is not None:
        return Bought(None, refused)
    try:
        async with session.begin_nested():
            alert_id = await session.scalar(
                insert(ActivityAlert)
                .values(
                    buyer_user_id=buyer_id,
                    target_user_id=target_id,
                    idempotency_key=key,
                    price=price,
                    created_at=now,
                )
                # The same Buy, or another alert for the same person, got there first.
                .on_conflict_do_nothing()
                .returning(ActivityAlert.id)
            )
            if alert_id is not None:
                await coins.debit(session, buyer_id, price, KIND, f"alert:{alert_id}")
    except InsufficientCoinsError:
        return Bought(None, Refusal.NO_COINS)
    if alert_id is None:
        same = await session.scalar(
            select(ActivityAlert.id).where(ActivityAlert.idempotency_key == key)
        )
        return (
            Bought(int(same), already=True) if same is not None else Bought(None, Refusal.PENDING)
        )
    return Bought(int(alert_id))


async def bought_under(session: AsyncSession, key: str) -> bool:
    found = await session.scalar(
        select(ActivityAlert.id).where(ActivityAlert.idempotency_key == key)
    )
    return found is not None


async def cancel(session: AsyncSession, alert_id: int, buyer_id: int, now: datetime) -> bool:
    """The buyer cancels an active alert, without a refund. Returns whether it did."""
    cancelled = await session.scalar(
        update(ActivityAlert)
        .where(
            ActivityAlert.id == alert_id,
            ActivityAlert.buyer_user_id == buyer_id,
            ActivityAlert.status == "active",
        )
        .values(status="cancelled", settled_at=now)
        .returning(ActivityAlert.id)
    )
    return cancelled is not None


# --- The worker ------------------------------------------------------------------------


def _blocked_either_way() -> ColumnElement[bool]:
    return exists().where(
        UserBlock.unblocked_at.is_(None),
        or_(
            and_(
                UserBlock.blocker_user_id == ActivityAlert.buyer_user_id,
                UserBlock.blocked_user_id == ActivityAlert.target_user_id,
            ),
            and_(
                UserBlock.blocker_user_id == ActivityAlert.target_user_id,
                UserBlock.blocked_user_id == ActivityAlert.buyer_user_id,
            ),
        ),
    )


def _opted_out() -> ColumnElement[bool]:
    return exists().where(
        NotificationSettings.user_id == ActivityAlert.target_user_id,
        NotificationSettings.activity_alerts == false(),
    )


def _target_gone(target: type[User]) -> ColumnElement[bool]:
    """The person can't use RealChat, or deleted the account after the purchase (a
    returning account is someone new)."""
    profile = aliased(Profile)
    unfinished = exists().where(
        profile.user_id == ActivityAlert.target_user_id,
        profile.onboarding_completed_at.is_(None),
    )
    return or_(
        target.status != "active",
        # Never NULL: NOT of a NULL would hide the alert from firing too.
        and_(target.deleted_at.is_not(None), target.deleted_at > ActivityAlert.created_at),
        unfinished,
    )


async def fire_due(session: AsyncSession, now: datetime, days: int) -> list[Settled]:
    """Fire every active alert, still within its `days`, whose person used RealChat after
    it was bought, unless it is due a refund instead. Returns those fired."""
    target = aliased(User)
    due = (
        select(ActivityAlert.id)
        .join(target, target.id == ActivityAlert.target_user_id)
        .where(
            ActivityAlert.status == "active",
            ActivityAlert.created_at >= now - timedelta(days=days),
            target.last_active_at > ActivityAlert.created_at,
            ~_target_gone(target),
            ~_blocked_either_way(),
            ~_opted_out(),
        )
        # An alert being refunded or cancelled right now is left for the next run.
        .with_for_update(skip_locked=True, of=ActivityAlert)
        .cte("due")
        .prefix_with("MATERIALIZED")
    )
    rows = await session.execute(
        update(ActivityAlert)
        .where(ActivityAlert.status == "active", ActivityAlert.id.in_(select(due.c.id)))
        .values(status="fired", settled_at=now)
        .returning(
            ActivityAlert.id,
            ActivityAlert.buyer_user_id,
            ActivityAlert.target_user_id,
            ActivityAlert.price,
        )
    )
    return [Settled(int(a), int(b), int(c), int(d)) for a, b, c, d in rows]


async def refund_candidates(
    session: AsyncSession, now: datetime, days: int
) -> list[tuple[int, int, str]]:
    """(alert, buyer, reason) for every active alert now due a refund."""
    target = aliased(User)
    reasons: list[tuple[str, ColumnElement[bool]]] = [
        ("unavailable", _target_gone(target)),
        ("blocked", _blocked_either_way()),
        ("opted_out", _opted_out()),
        ("expired", ActivityAlert.created_at < now - timedelta(days=days)),
    ]
    found: dict[int, tuple[int, int, str]] = {}
    for reason, condition in reasons:
        rows = await session.execute(
            select(ActivityAlert.id, ActivityAlert.buyer_user_id)
            .join(target, target.id == ActivityAlert.target_user_id)
            .where(ActivityAlert.status == "active", condition)
        )
        for alert_id, buyer_id in rows:
            found.setdefault(int(alert_id), (int(alert_id), int(buyer_id), reason))
    return list(found.values())


async def refund(
    session: AsyncSession, alert_id: int, buyer_id: int, reason: str, now: datetime
) -> Settled | None:
    """Refund one active alert; returns it if this call did.

    The buyer's account row is locked before the alert, the order account deletion
    uses, so the two never deadlock; the credit is keyed to the alert, so it is made
    once.
    """
    await session.execute(
        select(User.id).where(User.id == buyer_id).with_for_update(key_share=True)
    )
    row = (
        await session.execute(
            update(ActivityAlert)
            .where(
                ActivityAlert.id == alert_id,
                ActivityAlert.buyer_user_id == buyer_id,
                ActivityAlert.status == "active",
            )
            .values(status="refunded", refund_reason=reason, settled_at=now)
            .returning(ActivityAlert.target_user_id, ActivityAlert.price)
        )
    ).first()
    if row is None:
        return None
    target_id, price = int(row[0]), int(row[1])
    await coins.credit(session, buyer_id, price, REFUND_KIND, f"alert:{alert_id}:refund")
    return Settled(alert_id, buyer_id, target_id, price, reason)


class AlertNotifier(Protocol):
    async def fired(self, alert: Settled) -> None: ...

    async def refunded(self, alert: Settled) -> None: ...


class AlertJobs:
    """Fires and refunds alerts, then tells the buyers (best effort, after committing)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
        notifier: AlertNotifier,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime_settings
        self._notifier = notifier

    async def tick(self, now: datetime) -> int:
        """Returns how many alerts were settled."""
        async with self._sessions() as session:
            days = await self._runtime.get(session, SettingKey.ACTIVITY_ALERT_DAYS)
            fired = await fire_due(session, now, days)
            await session.commit()
        async with self._sessions() as session:
            candidates = await refund_candidates(session, now, days)
            await session.commit()
        refunded = []
        for alert_id, buyer_id, reason in candidates:
            # Each refund in its own transaction: one buyer's lock never holds up others.
            async with self._sessions() as session:
                settled = await refund(session, alert_id, buyer_id, reason, now)
                await session.commit()
            if settled is not None:
                refunded.append(settled)
        for alert in fired:
            await self._notify(self._notifier.fired, alert)
        for alert in refunded:
            await self._notify(self._notifier.refunded, alert)
        return len(fired) + len(refunded)

    async def _notify(self, notify: "AlertCall", alert: Settled) -> None:
        # One notice failing never keeps the others from being sent.
        try:
            await notify(alert)
        except Exception as exc:
            logger.warning("could not send an activity alert notice", extra=exception_summary(exc))


class AlertCall(Protocol):
    async def __call__(self, alert: Settled) -> None: ...
