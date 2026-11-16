"""Buying coin packs with Telegram Stars, /terms, and /paysupport (D-061).

/coins shows one button per pack. A tap stores an invoice and sends it; Telegram then
asks before taking the Stars (pre-checkout, answered within its 10-second limit) and
confirms the payment, which credits the coins. Only that confirmation grants
anything. Payment updates are never dropped by flood control or the restricted-account
gate (`apps.bot.middlewares.is_payment`). As elsewhere, the database transaction is
ended before any Telegram call.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.endings import coins_label
from apps.bot.onboarding import load_context
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import load_values
from backend.modules.payments import service as payments
from backend.modules.payments.service import Outcome, Pack, Refusal, UnknownPaymentError
from backend.modules.users.terms import TERMS_VERSION, terms_body

logger = logging.getLogger(__name__)

BUY_HEADER = "🛒 Buy coins with Telegram Stars:"
INVOICE_TITLE = "{coins} for RealChat"
INVOICE_DESCRIPTION = (
    "{coins} to spend in RealChat. They're added as soon as Telegram confirms the payment."
)
# A forwarded invoice opens the bot instead of offering a Pay button to someone else.
START_PARAMETER = "coins"
PACK_GONE = "This pack isn't available any more. Please choose again from /coins."
NOT_READY = "Please finish setting up your account first: send /start."
REFUSALS = {
    Refusal.UNKNOWN: "This invoice isn't valid. Please choose a pack again from /coins.",
    Refusal.MISMATCH: "This invoice isn't valid. Please choose a pack again from /coins.",
    Refusal.EXPIRED: "This offer has expired. Please choose again from /coins.",
    Refusal.UNAVAILABLE: "Your account can't make purchases right now.",
    Refusal.TERMS: "Please accept the updated terms first: send /start.",
}
CREDITED = "✅ Payment received: you got {coins}. Thank you!"
REFUNDED = (
    "Your RealChat account was deleted before this payment arrived, so your {stars} Stars "
    "were refunded."
)
UNMATCHED = (
    "We received your payment but couldn't match it to a purchase. Please send "
    "/paysupport so we can sort it out."
)
PAYSUPPORT = (
    "💬 Help with a purchase\n\n"
    "For any problem with a purchase in RealChat, contact RealChat support{where}. "
    "Please include the date and what you bought.\n\n"
    "Telegram's own support can't help with purchases made in RealChat."
)
SUPPORT_BUTTON = "💬 Contact support"


class PackAction(CallbackData, prefix="pack"):
    number: int


def _now() -> datetime:
    return datetime.now(UTC)


def pack_label(offer: Pack) -> str:
    return f"🪙 {coins_label(offer.coins)} · ⭐ {offer.stars}"


def pack_rows(offers: list[Pack]) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(
                text=pack_label(offer), callback_data=PackAction(number=offer.number).pack()
            )
        ]
        for offer in offers
    ]


async def on_pack(
    query: CallbackQuery, bot: Bot, callback_data: PackAction, session: AsyncSession
) -> None:
    ctx = await load_context(session, query.from_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await query.answer(NOT_READY)
        return
    # The current pack, not the one the button was drawn with: the invoice shows what
    # it will be honoured at.
    offer = payments.pack(await load_values(session), callback_data.number)
    if offer is None:
        await session.commit()
        await query.answer(PACK_GONE)
        return
    invoice_id = await payments.create_invoice(session, ctx.user.id, offer)
    await session.commit()
    await query.answer()
    coins = coins_label(offer.coins)
    await bot.send_invoice(
        query.from_user.id,
        title=INVOICE_TITLE.format(coins=coins),
        description=INVOICE_DESCRIPTION.format(coins=coins.capitalize()),
        payload=payments.payload(invoice_id),
        currency=payments.CURRENCY,
        prices=[LabeledPrice(label=coins, amount=offer.stars)],
        start_parameter=START_PARAMETER,
    )


async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot, session: AsyncSession) -> None:
    refusal = await payments.checkout_refusal(
        session,
        query.from_user.id,
        query.invoice_payload,
        query.currency,
        query.total_amount,
        _now(),
        TERMS_VERSION,
    )
    await session.commit()
    if refusal is None:
        await bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message=REFUSALS[refusal])


async def on_paid(message: Message, bot: Bot, session: AsyncSession) -> None:
    paid = message.successful_payment
    if paid is None or message.from_user is None:
        return
    try:
        fulfilled = await payments.fulfil(
            session,
            message.from_user.id,
            paid.telegram_payment_charge_id,
            paid.invoice_payload,
            paid.currency,
            paid.total_amount,
            _now(),
        )
    except UnknownPaymentError:
        await session.rollback()
        # The Stars were taken; support must look into it.
        logger.error("a confirmed payment matches no invoice")
        await bot.send_message(message.chat.id, UNMATCHED)
        return
    await session.commit()
    if fulfilled.outcome == Outcome.CREDITED:
        await bot.send_message(message.chat.id, CREDITED.format(coins=coins_label(fulfilled.coins)))
    elif fulfilled.status == "refund_due":
        await _refund(
            bot,
            session,
            message.from_user.id,
            paid.telegram_payment_charge_id,
            fulfilled.payment_id,
            paid.total_amount,
        )


async def _refund(
    bot: Bot,
    session: AsyncSession,
    telegram_user_id: int,
    charge_id: str,
    payment_id: int,
    stars: int,
) -> None:
    """Return the Stars of a payment that can't be fulfilled. A failure leaves it owed:
    a repeated confirmation tries again, and admins can see it."""
    try:
        await bot.refund_star_payment(
            user_id=telegram_user_id, telegram_payment_charge_id=charge_id
        )
    except TelegramAPIError as exc:
        logger.warning("could not refund a payment", extra=exception_summary(exc))
        return
    await payments.mark_refunded(session, payment_id, _now())
    await session.commit()
    await bot.send_message(telegram_user_id, REFUNDED.format(stars=stars))


async def show_terms(message: Message, settings: BotSettings) -> None:
    await message.answer(terms_body(settings.min_age))


async def show_paysupport(message: Message, settings: BotSettings) -> None:
    link = settings.support_contact
    await message.answer(
        PAYSUPPORT.format(where="" if link is None else " with the button below"),
        reply_markup=None
        if link is None
        else InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=SUPPORT_BUTTON, url=str(link))]]
        ),
    )


def create_router() -> Router:
    router = Router(name="payments")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_paid, F.successful_payment)
    router.message.register(show_terms, Command("terms"))
    router.message.register(show_paysupport, Command("paysupport"))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_pack, PackAction.filter())
    router.pre_checkout_query.register(on_pre_checkout)
    return router
