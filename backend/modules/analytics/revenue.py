"""Revenue analytics for admins (D-072).

Like the other services, these take an AsyncSession and leave committing to the
caller. Days are India calendar days. Stars are what users paid through Telegram,
not earnings: Telegram takes its share when Stars are withdrawn.

Purchases are counted on the day they were made, leaving out any payment refunded or
due for a refund (owner decision), so the figures show what was kept; refunds are
counted on the day they happened. Payments are kept when an account is deleted.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.analytics.periods import Period, Window, india_day
from backend.modules.moderation.review import audit
from backend.modules.payments.models import REFUND_REASONS, StarsInvoice, StarsPayment
from backend.modules.users.models import Profile, User


@dataclass(frozen=True)
class Bucket:
    start: date  # the first India day in the bucket
    coin_stars: int  # Stars for coin packs kept
    vip_stars: int  # Stars for VIP passes kept
    coin_packs: int
    vip_passes: int
    first_vip: int  # users whose first kept VIP pass was bought in the bucket
    refunds: dict[str, int]  # refunds made, by reason (every reason, zero included)
    refunded_stars: int


@dataclass(frozen=True)
class Totals:
    """All time, whatever the period."""

    stars: int  # kept
    refunds_due: int  # refunds owed and not yet made
    refunds_due_stars: int
    vip_buyers: int  # existing onboarded accounts with at least one kept VIP pass
    repeat_vip_buyers: int  # of those, with two or more
    onboarded: int  # existing onboarded accounts, for the conversion rate


@dataclass(frozen=True)
class Revenue:
    period: str
    bucket_days: int
    buckets: list[Bucket]
    totals: Totals


KEPT = StarsPayment.status == "paid"
KEPT_VIP = (
    KEPT,
    StarsPayment.invoice_id.in_(select(StarsInvoice.id).where(StarsInvoice.product == "vip")),
)


async def revenue(
    session: AsyncSession, actor_user_id: int, period: Period, now: datetime
) -> Revenue:
    """The revenue figures for `period`, ending today (India). The view is audited."""
    window = Window.of(period, now)
    purchases = await _purchases(session, window)
    first_vip = await _first_vip(session, window)
    refunds = await _refunds(session, window)
    buckets = []
    for start in window.starts:
        bought = purchases.get(start, {})
        refunded = refunds.get(start, {})
        buckets.append(
            Bucket(
                start=start,
                coin_stars=bought.get("coins", (0, 0))[1],
                vip_stars=bought.get("vip", (0, 0))[1],
                coin_packs=bought.get("coins", (0, 0))[0],
                vip_passes=bought.get("vip", (0, 0))[0],
                first_vip=first_vip.get(start, 0),
                refunds={reason: refunded.get(reason, (0, 0))[0] for reason in REFUND_REASONS},
                refunded_stars=sum(stars for _, stars in refunded.values()),
            )
        )
    session.add(audit(actor_user_id, "analytics_viewed", None, f"revenue {period.value}"))
    return Revenue(period.value, window.size, buckets, await _totals(session))


async def _purchases(
    session: AsyncSession, window: Window
) -> dict[date, dict[str, tuple[int, int]]]:
    """Kept purchases by bucket and product: (count, Stars)."""
    day = india_day(StarsPayment.created_at)
    rows = await session.execute(
        select(window.bucket(day), StarsInvoice.product, func.count(), func.sum(StarsPayment.stars))
        .join(StarsInvoice, StarsInvoice.id == StarsPayment.invoice_id)
        .where(KEPT, window.contains(day))
        .group_by(literal_column("1"), StarsInvoice.product)
    )
    found: dict[date, dict[str, tuple[int, int]]] = {}
    for start, product, count, stars in rows.all():
        found.setdefault(start, {})[product] = (int(count), int(stars))
    return found


async def _first_vip(session: AsyncSession, window: Window) -> dict[date, int]:
    first = (
        select(func.min(StarsPayment.created_at).label("at"))
        .where(*KEPT_VIP)
        .group_by(StarsPayment.user_id)
        .subquery()
    )
    day = india_day(first.c.at)
    rows = await session.execute(
        select(window.bucket(day), func.count())
        .select_from(first)
        .where(window.contains(day))
        .group_by(literal_column("1"))
    )
    return {row[0]: int(row[1]) for row in rows.all()}


async def _refunds(session: AsyncSession, window: Window) -> dict[date, dict[str, tuple[int, int]]]:
    """Refunds made, by bucket and reason: (count, Stars)."""
    day = india_day(StarsPayment.refunded_at)
    rows = await session.execute(
        select(
            window.bucket(day),
            StarsPayment.refund_reason,
            func.count(),
            func.sum(StarsPayment.stars),
        )
        .where(StarsPayment.status == "refunded", window.contains(day))
        .group_by(literal_column("1"), StarsPayment.refund_reason)
    )
    found: dict[date, dict[str, tuple[int, int]]] = {}
    for start, reason, count, stars in rows.all():
        if reason is not None:  # always set on a refund (a check constraint)
            found.setdefault(start, {})[reason] = (int(count), int(stars))
    return found


async def _totals(session: AsyncSession) -> Totals:
    stars, due, due_stars = (
        await session.execute(
            select(
                func.coalesce(func.sum(StarsPayment.stars).filter(KEPT), 0),
                func.count().filter(StarsPayment.status == "refund_due"),
                func.coalesce(
                    func.sum(StarsPayment.stars).filter(StarsPayment.status == "refund_due"), 0
                ),
            )
        )
    ).one()
    existing = (
        select(Profile.user_id)
        .join(User, User.id == Profile.user_id)
        .where(Profile.onboarding_completed_at.is_not(None), User.status != "deleted")
    )
    passes = (
        select(StarsPayment.user_id, func.count().label("passes"))
        .where(*KEPT_VIP, StarsPayment.user_id.in_(existing))
        .group_by(StarsPayment.user_id)
        .subquery()
    )
    buyers, repeat = (
        await session.execute(
            select(func.count(), func.count().filter(passes.c.passes >= 2)).select_from(passes)
        )
    ).one()
    onboarded = await session.scalar(select(func.count()).select_from(existing.subquery()))
    return Totals(
        stars=int(stars),
        refunds_due=int(due),
        refunds_due_stars=int(due_stars),
        vip_buyers=int(buyers),
        repeat_vip_buyers=int(repeat),
        onboarded=int(onboarded or 0),
    )
