"""Coin packs bought with Telegram Stars, /terms, and /paysupport (D-061)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import EditMessageReplyMarkup, SendMessage
from aiogram.types import InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import payments as bot_payments
from apps.bot.coins import CLAIM_BUTTON, LABELS
from apps.bot.main import create_dispatcher
from apps.bot.menu import BOT_COMMANDS
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.payments import service as payments
from backend.modules.payments.models import StarsInvoice, StarsPayment
from backend.modules.payments.service import Outcome, Refusal
from backend.modules.users import service as users
from backend.modules.users.models import TermsAcceptance, User
from backend.modules.users.terms import TERMS_VERSION, terms_parts
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked
from tests.support.telegram import make_bot, message_update, payment_update
from tests.support.world import buttons

Sessions = async_sessionmaker[AsyncSession]
PACK_1 = bot_payments.pack_label(payments.Pack(1, 150, 80))
REFUSED = bot_payments.REFUSALS
RESTRICT_VIOLATION = "23001"  # the guard trigger, not some other database error


async def _set(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        await session.merge(AppSetting(key=key.value, value=value))
        await session.commit()


async def _user_id(sessions: Sessions, telegram_id: int = USER_ID) -> int:
    async with sessions() as session:
        return (await users.get_or_create_user(session, telegram_id)).id


async def _balance(sessions: Sessions, telegram_id: int = USER_ID) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await coins.balance(session, user.id)).total


async def _purchases(sessions: Sessions) -> list[tuple[int, str]]:
    async with sessions() as session:
        rows = await session.execute(
            select(CoinTransaction.amount, CoinTransaction.idempotency_key)
            .where(CoinTransaction.kind == "coin_purchase")
            .order_by(CoinTransaction.id)
        )
        return [(int(amount), key) for amount, key in rows]


async def _payments(sessions: Sessions) -> list[tuple[str, str, str | None]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                StarsPayment.telegram_charge_id, StarsPayment.status, StarsPayment.refund_reason
            ).order_by(StarsPayment.id)
        )
        return [(charge, status, reason) for charge, status, reason in rows]


async def _set_status(sessions: Sessions, status: str) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status=status)
        )
        await session.commit()


async def _age_invoices(sessions: Sessions, by: timedelta) -> None:
    async with sessions() as session:
        await session.execute(update(StarsInvoice).values(created_at=StarsInvoice.created_at - by))
        await session.commit()


@pytest.fixture
async def chat(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)  # with the 10-coin welcome bonus
    return chat


async def _buy(chat: Chat, button: str = PACK_1) -> str:
    """Open /coins and tap a pack; returns the invoice's payload."""
    await chat.send("/coins")
    await chat.press(button)
    return chat.session.invoices()[-1].payload


def _answer(chat: Chat) -> tuple[bool, str | None]:
    answer = chat.session.checkout_answers()[-1]
    return answer.ok, answer.error_message


# --- Packs and invoices ----------------------------------------------------------------


async def test_coins_offers_each_pack_for_stars(chat: Chat) -> None:
    shown = await chat.send("/coins")
    assert shown.splitlines()[-2:] == ["", bot_payments.BUY_HEADER]
    assert buttons(chat.session.sent_to(USER_ID)[-1]) == [
        "🪙 150 coins · ⭐ 80",
        "🪙 350 coins · ⭐ 150",
        "🪙 800 coins · ⭐ 290",
        "🪙 1900 coins · ⭐ 580",
    ]


async def test_admins_can_change_and_hide_packs(chat: Chat, session_factory: Sessions) -> None:
    await _set(session_factory, SettingKey.PACK_2_COINS, 0)
    await _set(session_factory, SettingKey.PACK_3_STARS, 250)
    await chat.send("/coins")
    assert buttons(chat.session.sent_to(USER_ID)[-1]) == [
        "🪙 150 coins · ⭐ 80",
        "🪙 800 coins · ⭐ 250",
        "🪙 1900 coins · ⭐ 580",
    ]
    for key in (SettingKey.PACK_1_COINS, SettingKey.PACK_3_COINS, SettingKey.PACK_4_COINS):
        await _set(session_factory, key, 0)
    shown = await chat.send("/coins")
    assert bot_payments.BUY_HEADER not in shown
    assert chat.session.sent_to(USER_ID)[-1].reply_markup is None


async def test_a_pack_sends_a_stars_invoice_that_records_the_offer(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    invoice = chat.session.invoices()[-1]
    assert invoice.chat_id == USER_ID
    assert invoice.currency == "XTR"
    assert invoice.provider_token is None
    assert [(p.label, p.amount) for p in invoice.prices] == [("150 coins", 80)]
    # A forwarded copy opens the bot rather than offering someone else a Pay button.
    assert invoice.start_parameter == bot_payments.START_PARAMETER
    async with session_factory() as session:
        stored = (await session.execute(select(StarsInvoice))).scalar_one()
    assert payload == f"inv:{stored.id}"
    assert (stored.user_id, stored.product, stored.stars, stored.coins) == (
        await _user_id(session_factory),
        "coins",
        80,
        150,
    )


async def test_a_hidden_pack_button_sends_no_invoice(chat: Chat, session_factory: Sessions) -> None:
    await chat.send("/coins")
    await _set(session_factory, SettingKey.PACK_1_COINS, 0)
    await chat.press(PACK_1)
    assert chat.session.invoices() == []
    assert chat.session.callback_answers()[-1] == bot_payments.PACK_GONE


async def test_packs_need_onboarding(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await chat.send("/start")
    await chat.press_data(bot_payments.PackAction(number=1).pack())
    assert chat.session.invoices() == []
    assert chat.session.callback_answers()[-1] == bot_payments.NOT_READY


async def test_claiming_the_daily_reward_keeps_the_pack_buttons(
    chat: Chat, session_factory: Sessions
) -> None:
    await _set(session_factory, SettingKey.DAILY_REWARD_ON, 1)
    await chat.send("/coins")
    markup = chat.session.sent_to(USER_ID)[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    await chat.press_on(markup, CLAIM_BUTTON)
    edit = [m for m in chat.session.requests if isinstance(m, EditMessageReplyMarkup)][-1]
    assert isinstance(edit.reply_markup, InlineKeyboardMarkup)
    kept = [button.text for row in edit.reply_markup.inline_keyboard for button in row]
    assert CLAIM_BUTTON not in kept
    assert kept[0] == PACK_1 and len(kept) == 4


# --- Checkout --------------------------------------------------------------------------


async def test_checkout_approves_a_fresh_invoice(chat: Chat) -> None:
    payload = await _buy(chat)
    await chat.checkout(payload, 80)
    assert _answer(chat) == (True, None)


async def test_checkout_honours_the_invoice_after_a_price_change(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    await _set(session_factory, SettingKey.PACK_1_STARS, 100)
    await _set(session_factory, SettingKey.PACK_1_COINS, 120)
    await chat.checkout(payload, 80)
    assert _answer(chat) == (True, None)
    await chat.paid("charge-1", payload, 80)
    # The coins the invoice showed, not the new pack's.
    assert [amount for amount, _ in await _purchases(session_factory)] == [150]


@pytest.mark.parametrize(
    ("payload", "refusal"),
    [
        ("inv:999999", Refusal.UNKNOWN),
        ("garbage", Refusal.UNKNOWN),
        ("inv:", Refusal.UNKNOWN),
        ("inv:99999999999999999999", Refusal.UNKNOWN),
    ],
)
async def test_checkout_declines_an_invoice_that_is_not_ours(
    chat: Chat, payload: str, refusal: Refusal
) -> None:
    await chat.checkout(payload, 80)
    assert _answer(chat) == (False, REFUSED[refusal])


async def test_checkout_declines_a_changed_amount_or_currency(chat: Chat) -> None:
    payload = await _buy(chat)
    await chat.checkout(payload, 79)
    assert _answer(chat) == (False, REFUSED[Refusal.MISMATCH])
    await chat.checkout(payload, 80, currency="USD")
    assert _answer(chat) == (False, REFUSED[Refusal.MISMATCH])


async def test_checkout_declines_an_invoice_after_an_hour(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    await _age_invoices(session_factory, timedelta(minutes=59))
    await chat.checkout(payload, 80)
    assert _answer(chat) == (True, None)
    await _age_invoices(session_factory, timedelta(minutes=2))
    await chat.checkout(payload, 80)
    assert _answer(chat) == (False, REFUSED[Refusal.EXPIRED])


async def test_checkout_declines_someone_elses_invoice(
    chat: Chat, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    other = Chat(bot_settings, redis, session_factory, user_id=USER_ID + 1)
    await complete_onboarding(other)
    await other.checkout(payload, 80)
    assert _answer(other) == (False, REFUSED[Refusal.UNKNOWN])


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_checkout_declines_a_restricted_account_with_a_reason(
    chat: Chat, session_factory: Sessions, status: str
) -> None:
    payload = await _buy(chat)
    await _set_status(session_factory, status)
    await chat.checkout(payload, 80)
    assert _answer(chat) == (False, REFUSED[Refusal.UNAVAILABLE])


async def test_checkout_needs_the_current_terms(chat: Chat, session_factory: Sessions) -> None:
    payload = await _buy(chat)
    async with session_factory() as session:
        await session.execute(
            delete(TermsAcceptance).where(TermsAcceptance.terms_version == TERMS_VERSION)
        )
        await session.commit()
    await chat.checkout(payload, 80)
    assert _answer(chat) == (False, REFUSED[Refusal.TERMS])


async def test_checkout_declines_an_invoice_from_before_a_deletion(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    user_id = await _user_id(session_factory)
    async with session_factory() as session:
        await users.delete_account(session, user_id, datetime.now(UTC))
        await session.commit()
    await chat.checkout(payload, 80)
    assert _answer(chat) == (False, REFUSED[Refusal.UNKNOWN])
    # Back again: the old invoice still doesn't count.
    async with session_factory() as session:
        await users.accept_terms(session, user_id, TERMS_VERSION)
        await session.commit()
    await chat.checkout(payload, 80)
    assert _answer(chat) == (False, REFUSED[Refusal.UNKNOWN])


# --- Payment ---------------------------------------------------------------------------


async def test_a_payment_credits_the_coins_once(chat: Chat, session_factory: Sessions) -> None:
    payload = await _buy(chat)
    await chat.checkout(payload, 80)
    await chat.paid("charge-1", payload, 80)
    assert chat.last() == bot_payments.CREDITED.format(coins="150 coins")
    assert await _balance(session_factory) == 160
    assert await _payments(session_factory) == [("charge-1", "paid", None)]
    # Telegram delivering the confirmation again (a new update id) changes nothing.
    sent = len(chat.session.sent_texts())
    await chat.paid("charge-1", payload, 80)
    assert len(chat.session.sent_texts()) == sent
    assert await _balance(session_factory) == 160
    [(amount, key)] = await _purchases(session_factory)
    async with session_factory() as session:
        payment_id = await session.scalar(select(StarsPayment.id))
    assert (amount, key) == (150, f"payment:{payment_id}")


async def test_the_purchase_shows_in_coin_history(chat: Chat) -> None:
    payload = await _buy(chat)
    await chat.paid("charge-1", payload, 80)
    shown = await chat.send("/coins")
    assert any(
        line.startswith(f"+150  {LABELS['coin_purchase']} · ") for line in shown.splitlines()
    )


async def test_simultaneous_confirmations_credit_once(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)

    async def fulfil() -> Outcome:
        async with session_factory() as session:
            done = await payments.fulfil(
                session, USER_ID, "charge-1", payload, "XTR", 80, datetime.now(UTC)
            )
            await session.commit()
            return done.outcome

    outcomes = await asyncio.gather(fulfil(), fulfil())
    assert sorted(outcomes) == sorted([Outcome.CREDITED, Outcome.ALREADY])
    assert await _balance(session_factory) == 160


async def test_two_charges_for_one_invoice_each_credit(
    chat: Chat, session_factory: Sessions
) -> None:
    # Telegram never charges twice for one invoice message, but if it did, both
    # payments were real and both are honoured.
    payload = await _buy(chat)
    await chat.paid("charge-1", payload, 80)
    await chat.paid("charge-2", payload, 80)
    assert await _balance(session_factory) == 310


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_a_payment_after_a_restriction_is_still_credited(
    chat: Chat, session_factory: Sessions, status: str
) -> None:
    payload = await _buy(chat)
    await chat.checkout(payload, 80)
    await _set_status(session_factory, status)
    await chat.paid("charge-1", payload, 80)
    assert await _balance(session_factory) == 160
    assert chat.last() == bot_payments.CREDITED.format(coins="150 coins")


async def test_a_payment_after_a_deletion_is_refunded(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    await chat.checkout(payload, 80)
    async with session_factory() as session:
        await users.delete_account(session, await _user_id(session_factory), datetime.now(UTC))
        await session.commit()
    await chat.paid("charge-1", payload, 80)
    [refund] = chat.session.refunds()
    assert (refund.user_id, refund.telegram_payment_charge_id) == (USER_ID, "charge-1")
    assert await _payments(session_factory) == [("charge-1", "refunded", "account_deleted")]
    assert await _purchases(session_factory) == []
    assert chat.last() == bot_payments.REFUNDED.format(stars=80)
    # A repeated confirmation doesn't refund again.
    await chat.paid("charge-1", payload, 80)
    assert len(chat.session.refunds()) == 1


async def test_a_payment_during_a_deletion_waits_and_is_refunded(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    user_id = await _user_id(session_factory)
    async with session_factory() as deletion:
        await users.lock_user(deletion, user_id)
        await users.delete_account(deletion, user_id, datetime.now(UTC))

        async def confirm() -> Outcome:
            async with session_factory() as session:
                done = await payments.fulfil(
                    session, USER_ID, "charge-1", payload, "XTR", 80, datetime.now(UTC)
                )
                await session.commit()
                return done.outcome

        task = asyncio.create_task(confirm())
        await wait_until_blocked(session_factory)  # really overlapping
        await deletion.commit()
        # It saw the deletion, so nothing lands on the deleted account.
        assert await task == Outcome.REFUND_DUE
    assert await _purchases(session_factory) == []
    assert await _balance(session_factory) == 0


async def _refund_owed(chat: Chat, sessions: Sessions, error: str) -> str:
    """A payment for a deleted account whose immediate refund failed with `error`;
    returns its payload."""
    payload = await _buy(chat)
    async with sessions() as session:
        await users.delete_account(session, await _user_id(sessions), datetime.now(UTC))
        await session.commit()
    chat.session.refund_error = error
    await chat.paid("charge-1", payload, 80)
    assert await _payments(sessions) == [("charge-1", "refund_due", "account_deleted")]
    return payload


def _notices(chat: Chat) -> list[str]:
    return [m.text for m in chat.session.requests if isinstance(m, SendMessage)]


async def test_a_failed_refund_is_retried_on_the_next_confirmation(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _refund_owed(chat, session_factory, "Bad Request: temporarily unavailable")
    chat.session.refund_error = None
    await chat.paid("charge-1", payload, 80)
    assert len(chat.session.refunds()) == 2
    assert await _payments(session_factory) == [("charge-1", "refunded", "account_deleted")]


async def test_the_worker_retries_a_failed_refund(chat: Chat, session_factory: Sessions) -> None:
    await _refund_owed(chat, session_factory, "Bad Request: temporarily unavailable")
    retries = bot_payments.RefundRetries(chat.bot, session_factory)
    now = datetime.now(UTC)
    # Not before its retry time, which leaves the bot's own attempt alone.
    assert await retries.tick(now) == 0
    assert len(chat.session.refunds()) == 1
    # Still failing: it stays owed and waits again.
    later = now + payments.REFUND_RETRY
    assert await retries.tick(later) == 0
    assert len(chat.session.refunds()) == 2
    assert await retries.tick(later) == 0
    assert len(chat.session.refunds()) == 2
    chat.session.refund_error = None
    assert await retries.tick(later + payments.REFUND_RETRY) == 1
    [*_, refund] = chat.session.refunds()
    assert (refund.user_id, refund.telegram_payment_charge_id) == (USER_ID, "charge-1")
    assert await _payments(session_factory) == [("charge-1", "refunded", "account_deleted")]
    assert chat.last() == bot_payments.REFUNDED.format(stars=80)
    # Settled: never tried again.
    assert await retries.tick(later + 10 * payments.REFUND_RETRY) == 0
    assert len(chat.session.refunds()) == 3


async def test_a_refund_telegram_already_made_is_recorded_and_told_once(
    chat: Chat, session_factory: Sessions
) -> None:
    await _refund_owed(chat, session_factory, "Bad Request: temporarily unavailable")
    # An earlier call went through, but its result wasn't recorded, so nobody was told.
    chat.session.refund_error = "Bad Request: CHARGE_ALREADY_REFUNDED"
    retries = bot_payments.RefundRetries(chat.bot, session_factory)
    assert await retries.tick(datetime.now(UTC) + payments.REFUND_RETRY) == 1
    assert await _payments(session_factory) == [("charge-1", "refunded", "account_deleted")]
    assert _notices(chat).count(bot_payments.REFUNDED.format(stars=80)) == 1


async def test_a_refund_stands_when_its_notice_cant_be_sent(
    chat: Chat, session_factory: Sessions
) -> None:
    await _refund_owed(chat, session_factory, "Bad Request: temporarily unavailable")
    chat.session.refund_error = None
    chat.session.send_error = TelegramForbiddenError(
        method=SendMessage(chat_id=USER_ID, text="x"), message="Forbidden: bot was blocked"
    )
    retries = bot_payments.RefundRetries(chat.bot, session_factory)
    assert await retries.tick(datetime.now(UTC) + payments.REFUND_RETRY) == 1
    assert await _payments(session_factory) == [("charge-1", "refunded", "account_deleted")]
    [failed] = chat.session.failed_sends
    assert failed.text == bot_payments.REFUNDED.format(stars=80)
    assert failed.text not in _notices(chat)


async def test_simultaneous_workers_claim_a_refund_once(
    chat: Chat, session_factory: Sessions
) -> None:
    await _refund_owed(chat, session_factory, "Bad Request: temporarily unavailable")
    due = datetime.now(UTC) + payments.REFUND_RETRY
    async with session_factory() as first, session_factory() as second:
        [claimed] = await payments.claim_refunds(first, due, 10)
        assert claimed.charge_id == "charge-1"
        # The first claim isn't committed yet, and the row is skipped, not waited on.
        assert await payments.claim_refunds(second, due, 10) == []
        await first.commit()
        await second.rollback()
        # Committed: the next try is a full wait away.
        assert await payments.claim_refunds(second, due, 10) == []
        await second.rollback()


async def test_an_unmatched_payment_is_reported_to_the_user(
    chat: Chat, session_factory: Sessions
) -> None:
    payload = await _buy(chat)
    await chat.paid("charge-1", "inv:999999", 80)
    assert chat.last() == bot_payments.UNMATCHED
    await chat.paid("charge-2", payload, 81)
    assert chat.last() == bot_payments.UNMATCHED
    assert await _payments(session_factory) == []
    assert await _balance(session_factory) == 10


async def test_payments_pass_the_flood_limit(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    limited = bot_settings.model_copy(update={"bot_flood_limit": 3})
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    payload = await _buy(chat)
    bot, telegram = make_bot()
    dispatcher = create_dispatcher(limited, redis, session_factory, clock=lambda: 1_000_000.0)
    for update_id in range(5_000, 5_005):
        await dispatcher.feed_update(bot, message_update(update_id, USER_ID, "/coins"))
    await dispatcher.feed_update(bot, payment_update(5_100, USER_ID, "charge-1", payload, 80))
    assert telegram.sent_texts()[-1] == bot_payments.CREDITED.format(coins="150 coins")
    assert await _balance(session_factory) == 160


# --- /terms and /paysupport ------------------------------------------------------------


async def test_terms_and_paysupport_are_in_the_menu() -> None:
    names = [command.command for command in BOT_COMMANDS]
    assert "terms" in names and "paysupport" in names


async def test_terms_shows_the_current_terms(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await chat.send("/terms")
    parts = terms_parts(bot_settings.min_age)
    assert len(parts) > 1  # the terms are longer than one message
    assert chat.session.sent_texts() == parts


async def test_paysupport_points_to_support_not_telegram(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    with_link = bot_settings.model_copy(update={"support_contact": "https://t.me/realchat_support"})
    chat = Chat(with_link, redis, session_factory)
    shown = await chat.send("/paysupport")
    assert shown == bot_payments.PAYSUPPORT.format(where=" with the button below")
    assert "Telegram's own support can't help" in shown
    assert chat.session.last_buttons() == {
        bot_payments.SUPPORT_BUTTON: "https://t.me/realchat_support"
    }


@pytest.mark.parametrize("command", ["/terms", "/paysupport"])
async def test_restricted_users_can_read_terms_and_payment_support(
    chat: Chat, session_factory: Sessions, command: str
) -> None:
    await _set_status(session_factory, "banned")
    sent = len(chat.session.sent_texts())
    await chat.send(command)
    assert chat.session.sent_texts()[sent:] in (
        terms_parts(18),
        [bot_payments.PAYSUPPORT.format(where="")],
    )


# --- The payments table guard ------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("DELETE FROM stars_payments", "cannot be deleted"),
        ("TRUNCATE stars_payments", "cannot be deleted"),
        ("UPDATE stars_payments SET telegram_charge_id = 'other'", "can only move"),
        ("UPDATE stars_payments SET stars = 1", "can only move"),
        ("UPDATE stars_payments SET status = 'paid'", "can only move"),
    ],
)
async def test_a_payment_cant_be_deleted_or_rewritten(
    chat: Chat, session_factory: Sessions, statement: str, message: str
) -> None:
    payload = await _buy(chat)
    await chat.paid("charge-1", payload, 80)
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match=message) as raised:
            await session.execute(text(statement))
    assert str(raised.value.orig.sqlstate) == RESTRICT_VIOLATION  # type: ignore[union-attr]
    assert await _payments(session_factory) == [("charge-1", "paid", None)]


async def test_a_refunded_payment_cant_change_again(chat: Chat, session_factory: Sessions) -> None:
    payload = await _refund_owed(chat, session_factory, "Bad Request: temporarily unavailable")
    chat.session.refund_error = None
    await chat.paid("charge-1", payload, 80)
    for status in ("paid", "refund_due", "refunded"):
        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="can only move") as raised:
                await session.execute(
                    text("UPDATE stars_payments SET status = :status"), {"status": status}
                )
        assert str(raised.value.orig.sqlstate) == RESTRICT_VIOLATION  # type: ignore[union-attr]
