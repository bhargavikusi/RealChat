"""Activity alerts (D-059): buying, firing once, refunds, opt-out, and cancelling."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import activity_alerts as aa
from apps.bot.endings import ALERT_SET_BUTTON, BLOCK_BUTTON, AlertAction
from backend.config.settings import BotSettings
from backend.modules.activity_alerts import service as alerts
from backend.modules.activity_alerts.models import ActivityAlert
from backend.modules.activity_alerts.service import Refusal
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.users import service as users
from backend.modules.users.models import User
from backend.modules.users.service import Notification
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked
from tests.support.world import (
    ALERT,
    BOB_ID,
    World,
    buttons,
    last_to,
    onboarded_world,
    own_buttons,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
ALICE = "Night Owl"
BOB = "Moon Child"


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    """Alice and Bob, onboarded, who have never chatted."""
    return await onboarded_world(bot_settings, redis, session_factory)


async def public_id(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await users.get_profile(session, user.id)).public_id


async def user_id(sessions: Sessions, telegram_id: int) -> int:
    async with sessions() as session:
        return (await users.get_or_create_user(session, telegram_id)).id


async def ask(chat: Chat, sessions: Sessions, telegram_id: int) -> None:
    profile = await public_id(sessions, telegram_id)
    await chat.press_data(AlertAction(action="ask", profile=profile).pack())


async def buy(chat: Chat, sessions: Sessions, telegram_id: int) -> None:
    await ask(chat, sessions, telegram_id)
    await chat.press_data(own_buttons(chat)[aa.BUY_BUTTON])


def answer(chat: Chat) -> str:
    return chat.session.callback_answers()[-1]


def own_text(chat: Chat) -> str:
    return text_of(last_to(chat.session, chat.user_id))


async def balance(sessions: Sessions, telegram_id: int) -> int:
    async with sessions() as session:
        return (await coins.balance(session, await user_id(sessions, telegram_id))).available


async def entries(sessions: Sessions, kind: str) -> int:
    async with sessions() as session:
        found = await session.scalar(
            select(func.count()).select_from(CoinTransaction).where(CoinTransaction.kind == kind)
        )
        return int(found or 0)


async def rows(sessions: Sessions) -> list[tuple[str, str | None]]:
    async with sessions() as session:
        found = await session.execute(
            select(ActivityAlert.status, ActivityAlert.refund_reason).order_by(ActivityAlert.id)
        )
        return [(status, reason) for status, reason in found]


async def bob_uses_realchat(sessions: Sessions, after: timedelta = timedelta(seconds=1)) -> None:
    """Bob's RealChat activity is recorded just after the alerts were bought."""
    async with sessions() as session:
        latest = await session.scalar(select(func.max(ActivityAlert.created_at)))
        assert latest is not None
        await session.execute(
            update(User)
            .where(User.telegram_user_id == BOB_ID)
            .values(last_active_at=latest + after)
        )
        await session.commit()


async def move_back(sessions: Sessions, by: timedelta) -> None:
    """Move the alerts, and Bob's latest activity with them, back in time."""
    async with sessions() as session:
        await session.execute(
            update(ActivityAlert).values(created_at=ActivityAlert.created_at - by)
        )
        await session.execute(
            update(User)
            .where(User.telegram_user_id == BOB_ID)
            .values(last_active_at=User.last_active_at - by)
        )
        await session.commit()


def jobs(sessions: Sessions, chat: Chat) -> alerts.AlertJobs:
    """The worker's job, sending its notices through `chat`'s bot."""
    runtime = RuntimeSettings(ttl_seconds=0)
    return alerts.AlertJobs(sessions, runtime, aa.AlertNotices(chat.bot, sessions, runtime))


def now() -> datetime:
    return datetime.now(UTC)


async def set_setting(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        session.add(AppSetting(key=key.value, value=value))
        await session.commit()


async def set_status(sessions: Sessions, telegram_id: int, status: str) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == telegram_id).values(status=status)
        )
        await session.commit()


# --- Buying ------------------------------------------------------------------------------


async def test_buying_states_what_it_buys_and_charges_once(
    world: World, session_factory: Sessions
) -> None:
    before = await balance(session_factory, USER_ID)
    await ask(world.alice, session_factory, BOB_ID)
    assert own_text(world.alice) == aa.CONFIRM.format(nickname=BOB, days="7 days", price="1 coin")
    assert "not Telegram online status" in own_text(world.alice)
    data = own_buttons(world.alice)[aa.BUY_BUTTON]
    await world.alice.press_data(data)
    assert own_text(world.alice) == aa.BOUGHT.format(nickname=BOB, price="1 coin")
    await world.alice.press_data(data)
    assert answer(world.alice) == aa.ALREADY_BOUGHT
    assert await balance(session_factory, USER_ID) == before - 1
    assert await entries(session_factory, "activity_alert") == 1
    assert await rows(session_factory) == [("active", None)]
    # The card now offers Cancel instead.
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    card = buttons(last_to(world.alice.session, USER_ID))
    assert ALERT_SET_BUTTON in card and ALERT not in card


async def test_concurrent_purchases_for_one_person_charge_once(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await user_id(session_factory, USER_ID), await user_id(session_factory, BOB_ID)
    async with session_factory() as first, session_factory() as second:
        one = await alerts.buy(first, alice, bob, "a", 1, now())
        other = asyncio.create_task(alerts.buy(second, alice, bob, "b", 1, now()))
        await wait_until_blocked(session_factory)
        await first.commit()
        two = await other
        await second.commit()
    assert one.alert_id is not None
    assert two.alert_id is None and two.refusal == Refusal.PENDING
    assert await entries(session_factory, "activity_alert") == 1


async def test_the_price_shown_is_the_price_charged(
    world: World, session_factory: Sessions
) -> None:
    before = await balance(session_factory, USER_ID)
    await ask(world.alice, session_factory, BOB_ID)
    await set_setting(session_factory, SettingKey.ACTIVITY_ALERT_COINS, 3)
    world.alice.dispatcher["runtime_settings"].invalidate()
    await world.alice.press_data(own_buttons(world.alice)[aa.BUY_BUTTON])
    assert answer(world.alice) == aa.PRICE_CHANGED
    assert await balance(session_factory, USER_ID) == before
    assert await rows(session_factory) == []


async def test_not_enough_coins_is_refused(world: World, session_factory: Sessions) -> None:
    await set_setting(session_factory, SettingKey.ACTIVITY_ALERT_COINS, 11)
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == aa.NO_COINS.format(price="11 coins", available="10 coins")


async def test_coins_spent_meanwhile_leave_no_alert(
    world: World, session_factory: Sessions
) -> None:
    await ask(world.alice, session_factory, BOB_ID)
    alice = await user_id(session_factory, USER_ID)
    async with session_factory() as session:
        await coins.debit(session, alice, 10, "admin_correction", "drain", note="test")
        await session.commit()
    await world.alice.press_data(own_buttons(world.alice)[aa.BUY_BUTTON])
    assert answer(world.alice) == aa.REFUSALS[Refusal.NO_COINS]
    assert await rows(session_factory) == []
    assert await entries(session_factory, "activity_alert") == 0


async def test_someone_who_opted_out_cannot_be_watched(
    world: World, session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await users.set_notification(
            session, await user_id(session_factory, BOB_ID), Notification.ACTIVITY_ALERTS, False
        )
        await session.commit()
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == aa.REFUSALS[Refusal.OPTED_OUT].format(nickname=BOB)


async def test_blocks_stop_purchases_either_way(world: World, session_factory: Sessions) -> None:
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == aa.REFUSALS[Refusal.YOU_BLOCKED]
    await world.bob.send(f"/userid_{await public_id(session_factory, USER_ID)}")
    assert ALERT not in buttons(last_to(world.bob.session, BOB_ID))
    await ask(world.bob, session_factory, USER_ID)
    assert answer(world.bob) == aa.REFUSALS[Refusal.BLOCKED]
    assert await rows(session_factory) == []


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_an_unavailable_person_cannot_be_watched(
    world: World, session_factory: Sessions, status: str
) -> None:
    await set_status(session_factory, BOB_ID, status)
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == aa.REFUSALS[Refusal.UNAVAILABLE]


async def test_yourself_cannot_be_watched(world: World, session_factory: Sessions) -> None:
    await ask(world.alice, session_factory, USER_ID)
    assert answer(world.alice) == aa.REFUSALS[Refusal.UNAVAILABLE]


# --- Firing ------------------------------------------------------------------------------


async def test_one_alert_on_the_next_activity_labelled_as_realchat(
    world: World, session_factory: Sessions
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    job = jobs(session_factory, world.bob)
    assert await job.tick(now()) == 0  # Bob hasn't used RealChat since
    await bob_uses_realchat(session_factory)
    assert await job.tick(now()) == 1
    bob = await public_id(session_factory, BOB_ID)
    assert text_of(last_to(world.bob.session, USER_ID)) == aa.FIRED.format(
        nickname=BOB, public_id=bob
    )
    assert "not Telegram online status" in aa.FIRED
    assert await rows(session_factory) == [("fired", None)]
    # Once: it never fires again, and is never refunded after firing.
    await move_back(session_factory, timedelta(days=8))
    assert await job.tick(now()) == 0
    assert await entries(session_factory, "activity_alert_refund") == 0


async def test_activity_before_the_purchase_does_not_count(
    world: World, session_factory: Sessions
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    await bob_uses_realchat(session_factory, after=-timedelta(seconds=5))
    assert await jobs(session_factory, world.bob).tick(now()) == 0
    assert await rows(session_factory) == [("active", None)]


async def test_real_activity_through_the_bot_fires_it(
    world: World, session_factory: Sessions
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    # Bought a minute ago, and Bob's latest activity long enough before that for his next
    # update to be recorded.
    await move_back(session_factory, timedelta(minutes=5))
    await world.bob.send("/help")
    assert await jobs(session_factory, world.bob).tick(now()) == 1


# --- Refunds and cancelling ----------------------------------------------------------------


async def test_no_activity_in_time_refunds_once(world: World, session_factory: Sessions) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    before = await balance(session_factory, USER_ID)
    job = jobs(session_factory, world.bob)
    await move_back(session_factory, timedelta(days=6, hours=23))
    assert await job.tick(now()) == 0
    await move_back(session_factory, timedelta(hours=2))
    assert await job.tick(now()) == 1
    assert text_of(last_to(world.bob.session, USER_ID)) == aa.REFUND_EXPIRED.format(
        nickname=BOB, days="7 days", price="1 coin"
    )
    assert await rows(session_factory) == [("refunded", "expired")]
    assert await balance(session_factory, USER_ID) == before + 1
    # Running again refunds nothing more, even with activity now.
    await bob_uses_realchat(session_factory)
    assert await job.tick(now()) == 0
    assert await entries(session_factory, "activity_alert_refund") == 1


async def test_the_period_is_an_admin_setting(world: World, session_factory: Sessions) -> None:
    await set_setting(session_factory, SettingKey.ACTIVITY_ALERT_DAYS, 2)
    await buy(world.alice, session_factory, BOB_ID)
    await move_back(session_factory, timedelta(days=2, minutes=1))
    assert await jobs(session_factory, world.bob).tick(now()) == 1
    assert await rows(session_factory) == [("refunded", "expired")]


@pytest.mark.parametrize("blocker", [USER_ID, BOB_ID])
async def test_a_block_either_way_refunds_and_never_fires(
    world: World, session_factory: Sessions, blocker: int
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    other = BOB_ID if blocker == USER_ID else USER_ID
    blocking = world.alice if blocker == USER_ID else world.bob
    await blocking.send(f"/userid_{await public_id(session_factory, other)}")
    await blocking.press_data(own_buttons(blocking)[BLOCK_BUTTON])
    await bob_uses_realchat(session_factory)
    assert await jobs(session_factory, world.bob).tick(now()) == 1
    assert await rows(session_factory) == [("refunded", "blocked")]
    # The reason isn't given.
    assert text_of(last_to(world.bob.session, USER_ID)) == aa.REFUND_ENDED.format(
        nickname=BOB, price="1 coin"
    )


async def test_opting_out_refunds_pending_alerts(world: World, session_factory: Sessions) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    await world.bob.send("/profile")
    await world.bob.press("⚙️ Advanced settings")
    await world.bob.press("🔔 Activity alerts about me: On")
    await bob_uses_realchat(session_factory)  # refunded, not fired, though Bob is active
    assert await jobs(session_factory, world.bob).tick(now()) == 1
    assert await rows(session_factory) == [("refunded", "opted_out")]


@pytest.mark.parametrize("status", ["suspended", "deleted"])
async def test_an_unavailable_person_refunds(
    world: World, session_factory: Sessions, status: str
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    if status == "deleted":
        async with session_factory() as session:
            await users.delete_account(session, await user_id(session_factory, BOB_ID), now())
            await session.commit()
    else:
        await set_status(session_factory, BOB_ID, status)
    assert await jobs(session_factory, world.bob).tick(now()) == 1
    assert await rows(session_factory) == [("refunded", "unavailable")]


async def test_a_returning_account_is_someone_new(world: World, session_factory: Sessions) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    async with session_factory() as session:
        await users.delete_account(session, await user_id(session_factory, BOB_ID), now())
        await session.commit()
    await complete_onboarding(world.bob)  # Bob comes back as a new account
    await bob_uses_realchat(session_factory, after=timedelta(seconds=10))
    assert await jobs(session_factory, world.bob).tick(now()) == 1
    assert await rows(session_factory) == [("refunded", "unavailable")]


async def test_cancelling_ends_it_without_a_refund(world: World, session_factory: Sessions) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    before = await balance(session_factory, USER_ID)
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    cancel = own_buttons(world.alice)[ALERT_SET_BUTTON]
    await world.alice.press_data(cancel)
    assert answer(world.alice) == aa.CANCELLED
    await world.alice.press_data(cancel)
    assert answer(world.alice) == aa.NOT_SET
    await bob_uses_realchat(session_factory)
    await move_back(session_factory, timedelta(days=8))
    assert await jobs(session_factory, world.bob).tick(now()) == 0
    assert await rows(session_factory) == [("cancelled", None)]
    assert await balance(session_factory, USER_ID) == before


async def test_only_the_buyer_can_cancel(world: World, session_factory: Sessions) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    async with session_factory() as session:
        alert_id = int(await session.scalar(select(ActivityAlert.id)) or 0)
    await world.bob.press_data(AlertAction(action="cancel", alert=alert_id).pack())
    assert answer(world.bob) == aa.NOT_SET
    assert await rows(session_factory) == [("active", None)]


async def test_a_refund_is_made_once_however_often_it_runs(
    world: World, session_factory: Sessions
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    alice = await user_id(session_factory, USER_ID)
    async with session_factory() as session:
        alert_id = int(await session.scalar(select(ActivityAlert.id)) or 0)
    results = []
    for _ in range(2):
        async with session_factory() as session:
            results.append(await alerts.refund(session, alert_id, alice, "expired", now()))
            await session.commit()
    assert results[0] is not None and results[1] is None  # so the buyer is told once
    assert await entries(session_factory, "activity_alert_refund") == 1


async def test_a_deleted_buyers_alerts_are_erased_without_a_refund(
    world: World, session_factory: Sessions
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    async with session_factory() as session:
        await users.delete_account(session, await user_id(session_factory, USER_ID), now())
        await session.commit()
    assert await rows(session_factory) == []
    await move_back(session_factory, timedelta(days=8))
    assert await jobs(session_factory, world.bob).tick(now()) == 0
    assert await entries(session_factory, "activity_alert_refund") == 0


async def test_one_failing_notice_does_not_stop_the_others(
    world: World, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    carol = Chat(bot_settings, redis, session_factory, user_id=6464)
    await complete_onboarding(carol)
    await buy(world.alice, session_factory, BOB_ID)
    await buy(carol, session_factory, BOB_ID)
    await move_back(session_factory, timedelta(days=8))
    told: list[int] = []

    class Flaky:
        async def fired(self, alert: alerts.Settled) -> None: ...

        async def refunded(self, alert: alerts.Settled) -> None:
            if not told:
                told.append(0)
                raise RuntimeError("database hiccup")
            told.append(alert.buyer_id)

    job = alerts.AlertJobs(session_factory, RuntimeSettings(ttl_seconds=0), Flaky())
    assert await job.tick(now()) == 2
    assert len(told) == 2


async def test_activity_after_the_period_refunds_instead_of_firing(
    world: World, session_factory: Sessions
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    await move_back(session_factory, timedelta(days=8))
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == BOB_ID).values(last_active_at=now())
        )
        await session.commit()
    assert await jobs(session_factory, world.bob).tick(now()) == 1
    assert await rows(session_factory) == [("refunded", "expired")]


async def test_asking_again_while_an_alert_is_set_is_refused(
    world: World, session_factory: Sessions
) -> None:
    await buy(world.alice, session_factory, BOB_ID)
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == aa.REFUSALS[Refusal.PENDING].format(nickname=BOB)
    assert await entries(session_factory, "activity_alert") == 1
