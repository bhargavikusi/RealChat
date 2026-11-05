"""💰 /coins: the user's coin balance and recent activity (D-050).

Reached from Telegram's command menu (D-022). Shows the balance, any coins held for a
search that is still running, the most recent ledger entries, and the user's invite
link. As elsewhere, the
database transaction is ended before any Telegram call.
"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.onboarding import load_context, prompt_next
from backend.config.settings import BotSettings
from backend.modules.admin.settings import SettingKey, load_values
from backend.modules.coins import rewards
from backend.modules.coins import service as coins
from backend.modules.coins.service import Entry

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
    await session.commit()
    me = await bot.me()
    link = f"https://t.me/{me.username}?start={rewards.INVITE_PREFIX}{code}"
    invite = invite_lines(
        link, values[SettingKey.INVITE_REWARD_COINS], values[SettingKey.INVITE_REWARDS_PER_DAY]
    )
    await message.answer("\n".join([coins_text(balance, entries), *invite]))


def create_router() -> Router:
    router = Router(name="coins")
    # Private chats only: balances are never shown in a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_coins, Command("coins"))
    return router
