"""The VIP pass bought with Telegram Stars, and VIP status (D-062)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import payments as bot_payments
from apps.bot import vip as bot_vip
from apps.bot.menu import BOT_COMMANDS
from apps.bot.payments import vip_date
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.payments import service as payments
from backend.modules.payments.models import StarsInvoice, StarsPayment
from backend.modules.users import service as users
from backend.modules.users.models import User
from tests.support.chat import USER_ID, Chat, complete_onboarding

Sessions = async_sessionmaker[AsyncSession]
BUY = bot_vip.BUY_BUTTON.format(stars=780)
EXTEND = bot_vip.EXTEND_BUTTON.format(stars=780)
PASS = timedelta(days=180)
# Fulfilment runs a moment after the test reads the clock.
SLACK = timedelta(seconds=30)


async def _set(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        await session.merge(AppSetting(key=key.value, value=value))
        await session.commit()


async def _user(sessions: Sessions) -> User:
    async with sessions() as session:
        return await users.get_or_create_user(session, USER_ID)


async def _set_vip_until(sessions: Sessions, until: datetime | None) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(vip_until=until)
        )
        await session.commit()


async def _balance(sessions: Sessions) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, USER_ID)
        return (await coins.balance(session, user.id)).total


async def _vip_entries(sessions: Sessions) -> list[tuple[int, str]]:
    async with sessions() as session:
        rows = await session.execute(
            select(CoinTransaction.amount, CoinTransaction.idempotency_key)
            .where(CoinTransaction.kind == "vip_coins")
            .order_by(CoinTransaction.id)
        )
        return [(int(amount), key) for amount, key in rows]


@pytest.fixture
async def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)  # with the 10-coin welcome bonus
    return chat


async def _invoice(chat: Chat, button: str = BUY) -> str:
    """Open /vip and tap the button; returns the invoice's payload."""
    await chat.send("/vip")
    await chat.press(button)
    return chat.session.invoices()[-1].payload


async def _buy(chat: Chat, charge_id: str, button: str = BUY) -> None:
    payload = await _invoice(chat, button)
    await chat.checkout(payload, 780)
    assert chat.session.checkout_answers()[-1].ok
    await chat.paid(charge_id, payload, 780)


# --- /vip ------------------------------------------------------------------------------


async def test_vip_is_in_the_menu() -> None:
    assert "vip" in [c.command for c in BOT_COMMANDS]


async def test_vip_shows_the_status_perks_and_pass(chat: Chat) -> None:
    text = await chat.send("/vip")
    assert text == "\n".join(
        [
            bot_vip.HEADER,
            "",
            f"👑 {bot_vip.INACTIVE}",
            "",
            bot_vip.PERKS_HEADER,
            bot_vip.PERK_BADGE,
            bot_vip.PERK_PRIORITY,
            bot_vip.PERK_COINS.format(coins="3750 coins"),
            "",
            bot_vip.OFFER_TEXT.format(days=180, stars=780),
        ]
    )
    assert list(chat.session.last_buttons()) == [BUY]


async def test_a_pass_without_coins_lists_no_coin_perk(
    chat: Chat, session_factory: Sessions
) -> None:
    await _set(session_factory, SettingKey.VIP_COINS, 0)
    text = await chat.send("/vip")
    assert bot_vip.PERK_PRIORITY in text
    assert "coins added" not in text
    await _buy(chat, "charge-1")
    assert await _vip_entries(session_factory) == []
    until = (await _user(session_factory)).vip_until
    assert until is not None
    assert chat.last() == bot_payments.VIP_GRANTED.format(until=vip_date(until), coins="")


async def test_vip_needs_onboarding(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await chat.send("/vip")
    assert bot_vip.HEADER not in chat.last()
    assert chat.session.invoices() == []


async def test_coins_shows_the_vip_status(chat: Chat, session_factory: Sessions) -> None:
    assert f"👑 {bot_vip.INACTIVE} See /vip." in await chat.send("/coins")
    until = datetime.now(UTC) + timedelta(days=3)
    await _set_vip_until(session_factory, until)
    assert bot_vip.ACTIVE.format(until=vip_date(until)) in await chat.send("/coins")


# --- Buying ----------------------------------------------------------------------------


async def test_the_buy_button_sends_an_invoice_that_records_the_pass(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _invoice(chat)
    [invoice] = chat.session.invoices()
    assert (invoice.currency, [p.amount for p in invoice.prices]) == ("XTR", [780])
    assert invoice.title == bot_vip.INVOICE_TITLE.format(days=180)
    async with session_factory() as session:
        stored = await session.scalar(
            select(StarsInvoice).where(StarsInvoice.id == payments.invoice_id_from(payload))
        )
    assert stored is not None
    assert (stored.product, stored.stars, stored.days, stored.coins) == ("vip", 780, 180, 3_750)


async def test_a_payment_grants_vip_and_its_coins_once(
    chat: Chat, session_factory: Sessions
) -> None:
    before = datetime.now(UTC)
    payload = await _invoice(chat)
    await chat.checkout(payload, 780)
    await chat.paid("charge-1", payload, 780)
    user = await _user(session_factory)
    assert user.vip_until is not None
    assert before + PASS <= user.vip_until <= before + PASS + SLACK
    assert payments.is_vip(user, datetime.now(UTC))
    assert await _balance(session_factory) == 10 + 3_750
    async with session_factory() as session:
        payment = await session.scalar(select(StarsPayment))
    assert payment is not None
    assert payment.vip_until == user.vip_until
    assert await _vip_entries(session_factory) == [(3_750, f"payment:{payment.id}")]
    assert chat.last() == bot_payments.VIP_GRANTED.format(
        until=vip_date(user.vip_until),
        coins=bot_payments.VIP_COINS_ADDED.format(coins="3750 coins"),
    )
    # A repeated confirmation changes nothing.
    await chat.paid("charge-1", payload, 780)
    assert (await _user(session_factory)).vip_until == user.vip_until
    assert await _balance(session_factory) == 10 + 3_750


async def test_a_second_pass_extends_from_the_end_date(
    chat: Chat, session_factory: Sessions
) -> None:
    until = datetime.now(UTC) + timedelta(days=100)
    await _set_vip_until(session_factory, until)
    assert bot_vip.ACTIVE.format(until=vip_date(until)) in await chat.send("/vip")
    await _buy(chat, "charge-1", EXTEND)
    assert (await _user(session_factory)).vip_until == until + PASS


async def test_an_ended_vip_is_not_extended_from_its_old_end(
    chat: Chat, session_factory: Sessions
) -> None:
    await _set_vip_until(session_factory, datetime.now(UTC) - timedelta(days=3))
    before = datetime.now(UTC)
    await _buy(chat, "charge-1")
    until = (await _user(session_factory)).vip_until
    assert until is not None
    assert before + PASS <= until <= before + PASS + SLACK


async def test_the_invoice_is_honoured_at_the_days_it_showed(
    chat: Chat, session_factory: Sessions
) -> None:
    before = datetime.now(UTC)
    payload = await _invoice(chat)
    await _set(session_factory, SettingKey.VIP_DAYS, 30)
    await _set(session_factory, SettingKey.VIP_COINS, 5)
    await chat.checkout(payload, 780)
    await chat.paid("charge-1", payload, 780)
    until = (await _user(session_factory)).vip_until
    assert until is not None
    assert before + PASS <= until <= before + PASS + SLACK
    assert await _balance(session_factory) == 10 + 3_750


# --- The stacking limit ----------------------------------------------------------------


async def test_a_pass_can_be_bought_with_exactly_one_pass_left(
    chat: Chat, session_factory: Sessions
) -> None:
    # A minute of slack for the time between setting it and the checks.
    await _set_vip_until(session_factory, datetime.now(UTC) + PASS - timedelta(minutes=1))
    await chat.send("/vip")
    assert EXTEND in chat.session.last_buttons()


async def test_no_pass_while_more_than_one_pass_is_left(
    chat: Chat, session_factory: Sessions
) -> None:
    until = datetime.now(UTC) + PASS + timedelta(days=1)
    await _set_vip_until(session_factory, until)
    text = await chat.send("/vip")
    assert bot_vip.EXTEND_LATER.format(date=vip_date(until - PASS), days=180) in text
    assert EXTEND not in chat.session.last_buttons()
    # An older button is refused with when they can buy again.
    await chat.press_data(bot_vip.VipAction(action="buy").pack())
    assert chat.session.callback_answers()[-1] == bot_vip.TOO_EARLY.format(
        date=vip_date(until - PASS)
    )
    assert chat.session.invoices() == []


async def test_checkout_refuses_a_pass_once_too_much_vip_is_left(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _invoice(chat)
    await _set_vip_until(session_factory, datetime.now(UTC) + PASS + timedelta(days=1))
    await chat.checkout(payload, 780)
    answer = chat.session.checkout_answers()[-1]
    assert (answer.ok, answer.error_message) == (
        False,
        bot_payments.REFUSALS[payments.Refusal.VIP_LIMIT],
    )


async def test_two_passes_paid_meanwhile_are_both_granted(
    chat: Chat, session_factory: Sessions
) -> None:
    before = datetime.now(UTC)
    first = await _invoice(chat)
    second = await _invoice(chat)
    await chat.checkout(first, 780)
    await chat.checkout(second, 780)
    assert all(a.ok for a in chat.session.checkout_answers())
    await chat.paid("charge-1", first, 780)
    await chat.paid("charge-2", second, 780)
    until = (await _user(session_factory)).vip_until
    assert until is not None
    assert before + 2 * PASS <= until <= before + 2 * PASS + SLACK
    assert await _balance(session_factory) == 10 + 2 * 3_750


async def test_two_passes_confirmed_at_once_both_extend(
    chat: Chat, session_factory: Sessions
) -> None:
    # The user's row lock orders them, so neither overwrites the other's days.
    before = datetime.now(UTC)
    first = await _invoice(chat)
    second = await _invoice(chat)

    async def fulfil(charge_id: str, payload: str) -> datetime | None:
        async with session_factory() as session:
            done = await payments.fulfil(
                session, USER_ID, charge_id, payload, "XTR", 780, datetime.now(UTC)
            )
            await session.commit()
            return done.vip_until

    granted = await asyncio.gather(fulfil("charge-1", first), fulfil("charge-2", second))
    until = (await _user(session_factory)).vip_until
    assert until is not None
    assert before + 2 * PASS <= until <= before + 2 * PASS + SLACK
    assert max(g for g in granted if g is not None) == until
    assert await _balance(session_factory) == 10 + 2 * 3_750


# --- Expiry, restriction, and deletion -------------------------------------------------


def test_vip_ends_exactly_at_its_end_date() -> None:
    end = datetime(2027, 5, 1, 12, 0, tzinfo=UTC)
    user = User(telegram_user_id=1, vip_until=end)
    assert payments.is_vip(user, end - timedelta(microseconds=1))
    assert not payments.is_vip(user, end)
    assert not payments.is_vip(User(telegram_user_id=1), end)


async def test_coins_stay_when_vip_ends(chat: Chat, session_factory: Sessions) -> None:
    await _buy(chat, "charge-1")
    await _set_vip_until(session_factory, datetime.now(UTC) - timedelta(seconds=1))
    assert f"👑 {bot_vip.INACTIVE}" in await chat.send("/vip")
    assert list(chat.session.last_buttons()) == [BUY]
    assert await _balance(session_factory) == 10 + 3_750


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_a_payment_after_a_restriction_still_grants_vip(
    chat: Chat, session_factory: Sessions, status: str
) -> None:
    payload = await _invoice(chat)
    await chat.checkout(payload, 780)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status=status)
        )
        await session.commit()
    await chat.paid("charge-1", payload, 780)
    assert payments.is_vip(await _user(session_factory), datetime.now(UTC))
    assert await _balance(session_factory) == 10 + 3_750


async def test_a_payment_after_a_deletion_is_refunded_without_vip(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _invoice(chat)
    await chat.checkout(payload, 780)
    async with session_factory() as session:
        user = await users.get_or_create_user(session, USER_ID)
        await users.delete_account(session, user.id, datetime.now(UTC))
        await session.commit()
    await chat.paid("charge-1", payload, 780)
    assert len(chat.session.refunds()) == 1
    assert (await _user(session_factory)).vip_until is None
    assert await _vip_entries(session_factory) == []
    async with session_factory() as session:
        payment = await session.scalar(select(StarsPayment))
    assert payment is not None
    assert (payment.status, payment.vip_until) == ("refunded", None)


async def test_deleting_the_account_ends_vip_after_a_warning(
    chat: Chat, session_factory: Sessions
) -> None:
    await _buy(chat, "charge-1")
    until = (await _user(session_factory)).vip_until
    assert until is not None
    await chat.send("/profile")
    await chat.press(ac.ADVANCED_SETTINGS)
    warning = await chat.press(ac.DELETE_BUTTON)
    assert warning == ac.DELETE_CONFIRM.format(
        coins="3760 coins", vip=ac.VIP_LOST.format(until=vip_date(until))
    )
    await chat.press(ac.CONFIRM_BUTTON)
    assert (await _user(session_factory)).vip_until is None
    # The payment that bought it is kept, with what it granted.
    async with session_factory() as session:
        payment = await session.scalar(select(StarsPayment))
    assert payment is not None
    assert payment.vip_until == until
