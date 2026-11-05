"""💰 /coins: the user's coin balance and recent activity (D-050).

Reached from Telegram's command menu (D-022). Shows the balance, any coins held for a
search that is still running, the most recent ledger entries, the daily reward while
admins have it on (claimed with a button, once per UTC day), and the user's invite
link. As elsewhere, the
database transaction is ended before any Telegram call.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.onboarding import load_context, prompt_next
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import SettingKey, load_values
from backend.modules.coins import rewards
from backend.modules.coins import service as coins
from backend.modules.coins.service import Entry

logger = logging.getLogger(__name__)

HISTORY_LENGTH = 10
LABELS = {
    "welcome_bonus": "Welcome bonus",
    "referral_reward": "Invite reward",
    "daily_reward": "Daily reward",
    "paid_match": "Paid match",
    "admin_grant": "Bonus from RealChat",
    "admin_correction": "Correction",
}
NO_ACTIVITY = "No coin activity yet."
DAILY_AVAILABLE = "🎁 Daily reward: {amount} today."
DAILY_CLAIMED = "🎁 Daily reward claimed today. Come back tomorrow."
CLAIM_BUTTON = "🎁 Claim daily reward"
CLAIMED = "🎁 You got {amount}. Come back tomorrow for more."
ALREADY_CLAIMED = "Already claimed today. Come back tomorrow."
DAILY_OFF = "The daily reward isn't available right now."
NOT_AVAILABLE = "This button isn't available."


class DailyAction(CallbackData, prefix="daily"):
    action: str  # "claim"


def coin_count(amount: int) -> str:
    return f"{amount} coin{'s' if amount != 1 else ''}"


def daily_lines(on: bool, claimed: bool, amount: int) -> list[str]:
    if not on:
        return []
    return ["", DAILY_CLAIMED if claimed else DAILY_AVAILABLE.format(amount=coin_count(amount))]


def claim_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CLAIM_BUTTON, callback_data=DailyAction(action="claim").pack()
                )
            ]
        ]
    )


INVITE = "👥 Invite friends: {link}"
INVITE_REWARD = (
    "You get {amount} coins when someone you invite finishes creating their profile "
    "(up to {cap} a day)."
)


def invite_lines(link: str, amount: int, cap: int) -> list[str]:
    lines = ["", INVITE.format(link=link)]
    if amount and cap:
        lines.append(INVITE_REWARD.format(amount=amount, cap=cap))
    return lines


def coins_text(balance: coins.Balance, entries: list[Entry]) -> str:
    lines = ["💰 Your coins", "", f"Balance: {balance.total}"]
    if balance.held:
        lines += [f"Held for a search: {balance.held}", f"Available: {balance.available}"]
    lines += ["", "Recent activity:"]
    lines += [
        f"{entry.amount:+d}  {LABELS[entry.kind]} · {entry.created_at:%d %b}" for entry in entries
    ] or [NO_ACTIVITY]
    return "\n".join(lines)


async def on_coins(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None:
        return
    ctx = await load_context(session, message.from_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await prompt_next(bot, message.chat.id, session, ctx, state, settings)
        return
    await session.commit()
    # Any half-finished profile or admin edit is abandoned, so the next message is not
    # taken as its answer.
    await state.clear()
    # A fresh transaction, so the balance and history come from one snapshot.
    balance, entries = await coins.summary(session, ctx.user.id, HISTORY_LENGTH)
    await session.commit()
    code = await rewards.invite_code(session, ctx.user.id)
    values = await load_values(session)
    daily_on = bool(values[SettingKey.DAILY_REWARD_ON])
    claimed = daily_on and await rewards.claimed_today(session, ctx.user.id, datetime.now(UTC))
    await session.commit()
    me = await bot.me()
    link = f"https://t.me/{me.username}?start={rewards.INVITE_PREFIX}{code}"
    invite = invite_lines(
        link, values[SettingKey.INVITE_REWARD_COINS], values[SettingKey.INVITE_REWARDS_PER_DAY]
    )
    daily = daily_lines(daily_on, claimed, values[SettingKey.DAILY_REWARD_COINS])
    await message.answer(
        "\n".join([coins_text(balance, entries), *daily, *invite]),
        reply_markup=claim_keyboard() if daily_on and not claimed else None,
    )


async def on_claim(query: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    """🎁 Claim daily reward: once per UTC day, only while admins have it on."""
    ctx = await load_context(session, query.from_user)
    if not (ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None):
        await query.answer(NOT_AVAILABLE)
        return
    values = await load_values(session)
    amount = values[SettingKey.DAILY_REWARD_COINS]
    granted = bool(values[SettingKey.DAILY_REWARD_ON]) and await rewards.claim_daily(
        session, ctx.user.id, datetime.now(UTC), amount
    )
    on = bool(values[SettingKey.DAILY_REWARD_ON])
    await session.commit()
    await _remove_button(bot, query)
    if not on:
        await query.answer(DAILY_OFF)
    elif not granted:
        await query.answer(ALREADY_CLAIMED)
    else:
        await query.answer()
        await bot.send_message(query.from_user.id, CLAIMED.format(amount=coin_count(amount)))


async def _remove_button(bot: Bot, query: CallbackQuery) -> None:
    if query.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=None
        )
    except TelegramAPIError as exc:
        logger.info("could not remove the claim button", extra=exception_summary(exc))


def create_router() -> Router:
    router = Router(name="coins")
    # Private chats only: balances are never shown in a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_coins, Command("coins"))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_claim, DailyAction.filter(F.action == "claim"))
    return router
