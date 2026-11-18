"""/vip: the user's VIP status and the VIP pass, bought with Telegram Stars (D-062).

The pass is paid once and never renews. Buying while VIP is running adds its days to
the current end date, but only while at most one pass's length is left
(`payments.can_buy_vip`); checkout checks this again. Only perks that work today are
listed, so nothing is promised before it exists.
"""

from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.endings import coins_label
from apps.bot.onboarding import load_context, prompt_next
from apps.bot.payments import NOT_READY, send_invoice, vip_date
from backend.config.settings import BotSettings
from backend.modules.admin.settings import load_values
from backend.modules.payments import service as payments
from backend.modules.payments.service import VipPass
from backend.modules.users.models import User

HEADER = "👑 RealChat VIP"
ACTIVE = "You're VIP until {until}."
INACTIVE = "You're not VIP."
PERKS_HEADER = "What VIP includes:"
PERK_BADGE = (
    "• ✅ 👑 VIP after your nickname on your profile card and in People lists, and ✅ in "
    "notices. It shows you have VIP, not that your identity is verified."
)
PERK_PRIORITY = "• Matched first when you search, before people without VIP"
PERK_GHOST = (
    "• 👻 Ghost mode: open profiles without them seeing your visit. It starts on; switch "
    "it in ⚙️ Advanced settings."
)
PERK_COINS = "• {coins} added to your balance when you buy a pass"
OFFER_TEXT = (
    "A VIP pass gives {days} days of VIP for ⭐ {stars}. It's paid once and never renews; "
    "buying another while VIP is running adds the days to your end date. Coins you have "
    "stay yours when VIP ends."
)
EXTEND_LATER = "You can extend it from {date}, when {days} days or fewer are left."
BUY_BUTTON = "👑 Buy VIP · ⭐ {stars}"
EXTEND_BUTTON = "👑 Extend VIP · ⭐ {stars}"
TOO_EARLY = "You can extend VIP from {date}."
INVOICE_TITLE = "RealChat VIP: {days} days"
INVOICE_DESCRIPTION = (
    "{days} days of VIP in RealChat{coins}. Paid once; it doesn't renew. VIP starts as "
    "soon as Telegram confirms the payment."
)
INVOICE_COINS = ", plus {coins}"
DAYS_LABEL = "VIP for {days} days"


class VipAction(CallbackData, prefix="vip"):
    action: str  # "buy"


def status_line(user: User, now: datetime) -> str:
    """The user's VIP status, also shown on /coins."""
    if payments.is_vip(user, now) and user.vip_until is not None:
        return ACTIVE.format(until=vip_date(user.vip_until))
    return INACTIVE


def perk_lines(offer: VipPass) -> list[str]:
    """The perks that work today; each VIP perk adds its line when it is built."""
    perks = [PERK_BADGE, PERK_PRIORITY, PERK_GHOST]
    if offer.coins:
        perks.append(PERK_COINS.format(coins=coins_label(offer.coins)))
    return perks


def _extend_from(user: User, offer: VipPass, now: datetime) -> datetime:
    """When another pass can be bought: one pass's length before VIP ends."""
    return (user.vip_until or now) - timedelta(days=offer.days)


def vip_text(user: User, offer: VipPass, now: datetime) -> str:
    lines = [HEADER, "", f"👑 {status_line(user, now)}"]
    lines += ["", PERKS_HEADER, *perk_lines(offer)]
    lines += ["", OFFER_TEXT.format(days=offer.days, stars=offer.stars)]
    if not payments.can_buy_vip(user, now, offer.days):
        lines += [
            "",
            EXTEND_LATER.format(date=vip_date(_extend_from(user, offer, now)), days=offer.days),
        ]
    return "\n".join(lines)


def vip_keyboard(user: User, offer: VipPass, now: datetime) -> InlineKeyboardMarkup | None:
    if not payments.can_buy_vip(user, now, offer.days):
        return None
    label = EXTEND_BUTTON if payments.is_vip(user, now) else BUY_BUTTON
    button = InlineKeyboardButton(
        text=label.format(stars=offer.stars), callback_data=VipAction(action="buy").pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def on_vip(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None:
        return
    ctx = await load_context(session, message.from_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await prompt_next(bot, message.chat.id, session, ctx, state, settings)
        return
    # Any half-finished profile or admin edit is abandoned, so the next message is not
    # taken as its answer.
    await state.clear()
    offer = payments.vip_pass(await load_values(session))
    await session.commit()
    now = datetime.now(UTC)
    await message.answer(
        vip_text(ctx.user, offer, now), reply_markup=vip_keyboard(ctx.user, offer, now)
    )


async def on_buy(query: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    ctx = await load_context(session, query.from_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await query.answer(NOT_READY)
        return
    # The current pass, not the one the button was drawn with: the invoice shows what
    # it will be honoured at.
    offer = payments.vip_pass(await load_values(session))
    now = datetime.now(UTC)
    if not payments.can_buy_vip(ctx.user, now, offer.days):
        await session.commit()
        await query.answer(
            TOO_EARLY.format(date=vip_date(_extend_from(ctx.user, offer, now))), show_alert=True
        )
        return
    invoice_id = await payments.create_invoice(session, ctx.user.id, offer)
    await session.commit()
    await query.answer()
    coins = INVOICE_COINS.format(coins=coins_label(offer.coins)) if offer.coins else ""
    await send_invoice(
        bot,
        query.from_user.id,
        invoice_id,
        INVOICE_TITLE.format(days=offer.days),
        INVOICE_DESCRIPTION.format(days=offer.days, coins=coins),
        DAYS_LABEL.format(days=offer.days),
        offer.stars,
    )


def create_router() -> Router:
    router = Router(name="vip")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_vip, Command("vip"))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_buy, VipAction.filter(F.action == "buy"))
    return router
