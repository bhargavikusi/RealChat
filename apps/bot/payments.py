"""Buying coin packs and the VIP pass with Telegram Stars, /terms, and /paysupport
(D-061, D-062).

/coins shows one button per pack, and /vip the pass (`apps/bot/vip.py`). A tap stores an
invoice and sends it; Telegram then
asks before taking the Stars (pre-checkout, answered within its 10-second limit) and
confirms the payment, which credits a pack's coins, or grants a pass's VIP days and
any coins it includes. Only that confirmation grants
anything. Payment updates are never dropped by flood control or the restricted-account
gate (`apps.bot.middlewares.is_payment`). As elsewhere, the database transaction is
ended before any Telegram call.

A payment that can't be fulfilled is refunded at once; if Telegram refuses or can't be
reached, the worker retries it (`RefundRetries`), since Telegram doesn't send the
confirmation again. The worker also sends refunds an admin made (D-065). When Telegram
reports a payment refunded, the purchase is taken back (`on_refunded`). Whichever
records a refund first tells the user, once.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.endings import coins_label
from apps.bot.menu import send_parts
from apps.bot.onboarding import load_context
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import load_values
from backend.modules.payments import service as payments
from backend.modules.payments.service import (
    Outcome,
    Pack,
    RefundDue,
    RefundOutcome,
    Refusal,
    UnknownPaymentError,
)
from backend.modules.users.terms import TERMS_VERSION, terms_parts

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
    Refusal.VIP_LIMIT: "You already have plenty of VIP time left. See /vip for when you "
    "can extend it.",
}
CREDITED = "✅ Payment received: you got {coins}. Thank you!"
VIP_GRANTED = "👑 Payment received: you're VIP until {until}.{coins} Thank you!"
VIP_COINS_ADDED = " {coins} were added to your balance."
REFUNDED = (
    "Your RealChat account was deleted before this payment arrived, so your {stars} Stars "
    "were refunded."
)
# A fulfilled purchase refunded (D-065); the parts after the first are added as they apply.
REFUND_NOTICE = "💸 Your payment of ⭐ {stars} was refunded."
REFUND_COINS = " {coins} were removed from your balance."
REFUND_VIP_LEFT = " Your VIP now lasts until {until}."
REFUND_VIP_ENDED = " You don't have VIP any more."
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
# Telegram's answer when the charge was refunded before, for example by an earlier
# attempt whose result wasn't recorded.
ALREADY_REFUNDED = "CHARGE_ALREADY_REFUNDED"
REFUND_BATCH = 20


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
    await send_invoice(
        bot,
        query.from_user.id,
        invoice_id,
        INVOICE_TITLE.format(coins=coins),
        INVOICE_DESCRIPTION.format(coins=coins.capitalize()),
        coins,
        offer.stars,
    )


async def send_invoice(
    bot: Bot, chat_id: int, invoice_id: int, title: str, description: str, label: str, stars: int
) -> None:
    """Send a stored invoice; call with no transaction open."""
    await bot.send_invoice(
        chat_id,
        title=title,
        description=description,
        payload=payments.payload(invoice_id),
        currency=payments.CURRENCY,
        prices=[LabeledPrice(label=label, amount=stars)],
        start_parameter=START_PARAMETER,
    )


def vip_date(moment: datetime) -> str:
    return f"{moment.day} {moment:%B %Y}, {moment:%H:%M} UTC"


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
    if fulfilled.outcome == Outcome.CREDITED and fulfilled.vip_until is not None:
        added = VIP_COINS_ADDED.format(coins=coins_label(fulfilled.coins).capitalize())
        await bot.send_message(
            message.chat.id,
            VIP_GRANTED.format(
                until=vip_date(fulfilled.vip_until), coins=added if fulfilled.coins else ""
            ),
        )
    elif fulfilled.outcome == Outcome.CREDITED:
        await bot.send_message(message.chat.id, CREDITED.format(coins=coins_label(fulfilled.coins)))
    elif fulfilled.status == "refund_due":
        due = RefundDue(
            fulfilled.payment_id,
            message.from_user.id,
            paid.telegram_payment_charge_id,
            paid.total_amount,
            fulfilled.refund_reason or "account_deleted",
        )
        await refund(bot, session, due)


async def refund(bot: Bot, session: AsyncSession, due: RefundDue) -> bool:
    """Return the Stars of a payment that can't be fulfilled; returns whether it is now
    refunded. A failure leaves it owed for the worker to retry. Call with no
    transaction open."""
    try:
        await bot.refund_star_payment(
            user_id=due.telegram_user_id, telegram_payment_charge_id=due.charge_id
        )
    except TelegramAPIError as exc:
        if ALREADY_REFUNDED not in exc.message:
            logger.warning("could not refund a payment", extra=exception_summary(exc))
            return False
        # Refunded by an earlier call whose result wasn't recorded, so nobody was told.
    # Only the call that records the refund tells the user: Telegram's own report of it
    # may have recorded it, and told them, first.
    if await payments.mark_refunded(session, due.payment_id, _now()):
        text = await _refund_text(session, due.payment_id, due.reason)
        await session.commit()
        await _tell(bot, due.telegram_user_id, text)
    else:
        await session.commit()
    return True


async def _refund_text(session: AsyncSession, payment_id: int, reason: str | None) -> str:
    summary = await payments.refund_summary(session, payment_id, _now())
    if reason == "account_deleted":
        return REFUNDED.format(stars=summary.stars)
    text = REFUND_NOTICE.format(stars=summary.stars)
    if summary.coins:
        text += REFUND_COINS.format(coins=coins_label(summary.coins))
    if summary.product == "vip":
        text += (
            REFUND_VIP_ENDED
            if summary.vip_until is None
            else REFUND_VIP_LEFT.format(until=vip_date(summary.vip_until))
        )
    return text


async def _tell(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as exc:
        # The refund stands; the user may have blocked the bot after deleting.
        logger.info("could not send a refund notice", extra=exception_summary(exc))


async def on_refunded(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Telegram reports a payment refunded, by us or for example after a dispute."""
    refunded = message.refunded_payment
    if refunded is None or message.from_user is None:
        return
    recorded = await payments.record_refund(
        session, message.from_user.id, refunded.telegram_payment_charge_id, _now()
    )
    if recorded.outcome == RefundOutcome.UNKNOWN:
        await session.rollback()
        logger.error("a refunded payment matches no payment")
        return
    if recorded.outcome == RefundOutcome.ALREADY:
        await session.commit()
        return
    text = await _refund_text(session, recorded.payment_id, recorded.reason)
    await session.commit()
    await _tell(bot, message.chat.id, text)


class RefundRetries:
    """Retries refunds still owed (D-061). Runs in the worker.

    Each tick claims a bounded batch whose retry time has come; a claim moves the next
    retry `payments.REFUND_RETRY` ahead, so a refund Telegram keeps refusing is tried
    again at that pace, and only the failure is logged, without user data.
    """

    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions

    async def tick(self, now: datetime) -> int:
        """Try one batch; returns how many were refunded."""
        async with self._sessions() as session:
            claimed = await payments.claim_refunds(session, now, REFUND_BATCH)
            await session.commit()
            refunded = 0
            for due in claimed:
                refunded += await refund(self._bot, session, due)
        if refunded:
            logger.info("refunded payments", extra={"count": refunded})
        return refunded


async def show_terms(message: Message, bot: Bot, settings: BotSettings) -> None:
    await send_parts(bot, message.chat.id, terms_parts(settings.min_age))


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
    router.message.register(on_refunded, F.refunded_payment)
    router.message.register(show_terms, Command("terms"))
    router.message.register(show_paysupport, Command("paysupport"))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_pack, PackAction.filter())
    router.pre_checkout_query.register(on_pre_checkout)
    return router
