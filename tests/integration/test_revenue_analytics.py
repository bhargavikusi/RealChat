"""Revenue analytics for admins (D-072)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.admin.models import AdminAuditEntry
from backend.modules.analytics.periods import ZONE
from backend.modules.payments.models import StarsInvoice, StarsPayment
from backend.modules.users import service as users
from tests.support.moderation import ADMIN, ALICE, MODERATOR, Panel
from tests.support.webapp import authorization

Sessions = async_sessionmaker[AsyncSession]
DAY = timedelta(days=1)


def _get(panel: Panel, as_user: int = ADMIN, **params: Any) -> Any:
    return panel.client.get(
        "/api/moderation/analytics/revenue",
        params=params,
        headers=authorization(as_user, datetime.now(UTC)),
    )


async def _pay(
    sessions: Sessions,
    user_id: int,
    product: str,
    stars: int,
    at: datetime,
    status: str = "paid",
    refunded_at: datetime | None = None,
    reason: str | None = None,
) -> None:
    async with sessions() as session:
        vip = product == "vip"
        invoice = StarsInvoice(
            user_id=user_id,
            product=product,
            stars=stars,
            coins=0 if vip else stars * 5,
            days=180 if vip else None,
        )
        session.add(invoice)
        await session.flush()
        session.add(
            StarsPayment(
                telegram_charge_id=f"charge-{invoice.id}",
                user_id=user_id,
                invoice_id=invoice.id,
                stars=stars,
                status=status,
                created_at=at,
                refunded_at=refunded_at,
                refund_reason=reason,
                refund_note="test refund" if reason == "admin" else None,
                refund_retry_at=at if status == "refund_due" else None,
                vip_until=at + timedelta(days=180) if vip else None,
            )
        )
        await session.commit()


async def test_only_admins_see_revenue_analytics(panel: Panel, people: dict[str, int]) -> None:
    assert _get(panel, MODERATOR, period="7d").status_code == 403
    assert _get(panel, ALICE, period="7d").status_code == 403
    assert _get(panel, period="week").status_code == 422
    assert _get(panel).status_code == 422


async def test_revenue_figures(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    today = now.astimezone(ZONE).date()
    alice, bob, carol, moderator = (
        people["alice"],
        people["bob"],
        people["carol"],
        people["moderator"],
    )
    # Alice: a pass today and one before the period, so a repeat buyer.
    await _pay(session_factory, alice, "vip", 780, now)
    await _pay(session_factory, alice, "vip", 780, now - 20 * DAY)
    # Bob: his first pass two days ago, a coin pack today, and a coin pack yesterday
    # refunded by an admin today.
    await _pay(session_factory, bob, "vip", 780, now - 2 * DAY)
    await _pay(session_factory, bob, "coins", 100, now)
    await _pay(session_factory, bob, "coins", 250, now - DAY, "refunded", now, "admin")
    # Carol: a pass with a refund still due, and a coin pack refunded by Telegram.
    await _pay(session_factory, carol, "vip", 780, now - DAY, "refund_due", reason="telegram")
    await _pay(
        session_factory, carol, "coins", 50, now - 3 * DAY, "refunded", now - 2 * DAY, "telegram"
    )
    # The moderator: a pass five days ago, then the account is deleted; and a pass
    # refunded before the period.
    await _pay(session_factory, moderator, "vip", 780, now - 5 * DAY)
    await _pay(
        session_factory,
        moderator,
        "vip",
        780,
        now - 30 * DAY,
        "refunded",
        now - 10 * DAY,
        "telegram",
    )
    async with session_factory() as session:
        await users.delete_account(session, moderator, now)
        await session.commit()

    response = _get(panel, period="7d")

    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["period"], data["bucket_days"], len(data["buckets"])) == ("7d", 1, 7)
    by_day = {bucket["start"]: bucket for bucket in data["buckets"]}

    def day(days_ago: int) -> dict[str, Any]:
        found = dict(by_day[(today - timedelta(days=days_ago)).isoformat()])
        found.pop("start")
        return found

    none = {"account_deleted": 0, "admin": 0, "telegram": 0}
    empty = {
        "coin_stars": 0,
        "vip_stars": 0,
        "coin_packs": 0,
        "vip_passes": 0,
        "first_vip": 0,
        "refunds": none,
        "refunded_stars": 0,
    }
    assert day(0) == {
        **empty,
        "coin_stars": 100,
        "vip_stars": 780,
        "coin_packs": 1,
        "vip_passes": 1,
        "refunds": {**none, "admin": 1},
        "refunded_stars": 250,
    }
    assert day(1) == empty  # Bob's refunded pack and Carol's refund due aren't kept
    assert day(2) == {
        **empty,
        "vip_stars": 780,
        "vip_passes": 1,
        "first_vip": 1,
        "refunds": {**none, "telegram": 1},
        "refunded_stars": 50,
    }
    assert day(3) == empty
    assert day(5) == {**empty, "vip_stars": 780, "vip_passes": 1, "first_vip": 1}

    assert data["totals"] == {
        "stars": 780 * 4 + 100,
        "refunds_due": 1,
        "refunds_due_stars": 780,
        # Alice and Bob; the moderator's account is deleted.
        "vip_buyers": 2,
        "repeat_vip_buyers": 1,
        # Alice, Bob, Carol, and the admin.
        "onboarded": 4,
    }


async def test_viewing_revenue_analytics_is_audited(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    assert _get(panel, period="12m").status_code == 200
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AdminAuditEntry.actor_user_id, AdminAuditEntry.new_value).where(
                    AdminAuditEntry.action == "analytics_viewed"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [(people["admin"], "revenue 12m")]


async def test_older_rows_stay_out_of_the_first_week(
    panel: Panel, people: dict[str, int], session_factory: Sessions
) -> None:
    # A year is 52 weeks ending today: these fall three days before its first day.
    before = datetime.now(UTC) - 366 * DAY
    await _pay(session_factory, people["bob"], "coins", 100, before)
    await _pay(session_factory, people["carol"], "vip", 780, before)
    await _pay(
        session_factory, people["alice"], "coins", 50, before - DAY, "refunded", before, "telegram"
    )
    buckets = _get(panel, period="12m").json()["buckets"]
    assert len(buckets) == 52
    assert all(bucket["coin_packs"] == 0 for bucket in buckets)
    assert all(bucket["first_vip"] == 0 for bucket in buckets)
    assert all(bucket["refunded_stars"] == 0 for bucket in buckets)
