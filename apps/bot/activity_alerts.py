"""Activity alerts: 🔔 Alert when active on a profile card (D-059).

1. 🔔 Alert when active on another user's card states exactly what it buys: one alert
   the next time the person uses RealChat, within the period (an admin setting), for
   the price, returned if they don't. It is RealChat activity, never Telegram online
   status. Buy takes the price at once; a repeated Buy charges nothing more.
2. While an alert is set, the card offers Cancel instead; cancelling returns nothing.
3. The worker alerts the buyer once, or refunds them (`AlertNotices` tells them). The
   person is never told who set an alert, and can turn alerts about them off in
   Advanced settings.

The wording and layout are provisional until the design pass. As elsewhere, every
database transaction is ended before any Telegram call.
"""

import logging
import secrets
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.endings import AlertAction, coins_label
from apps.bot.people import NOT_AVAILABLE, days_label, ready_user
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.activity_alerts import service as alerts
from backend.modules.activity_alerts.service import Refusal, Settled
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.coins import service as coins
from backend.modules.users import service as users

logger = logging.getLogger(__name__)

CONFIRM = (
    "🔔 Get one alert the next time {nickname} uses RealChat, within {days}, for {price}? "
    "If they don't, it's returned. This is activity in RealChat, not Telegram online status."
)
BUY_BUTTON = "🔔 Buy alert"
DISMISS_BUTTON = "Cancel"
DISMISSED = "Cancelled."
REFUSALS = {
    Refusal.UNAVAILABLE: NOT_AVAILABLE,
    Refusal.BLOCKED: "You can't set an alert for this person.",
    Refusal.YOU_BLOCKED: "You blocked this person. Unblock them to set an alert.",
    Refusal.OPTED_OUT: "{nickname} doesn't allow activity alerts.",
    Refusal.PENDING: "You already have an alert set for {nickname}.",
    Refusal.NO_COINS: "You don't have enough coins for this alert.",
}
NO_COINS = "You need {price} for this alert, and you have {available}."
PRICE_CHANGED = "The price has changed. Please tap 🔔 Alert when active again."
ALREADY_BOUGHT = "Already bought."
BOUGHT = (
    "🔔 Alert set for {nickname}. {price} used. You'll be told the next time they use RealChat."
)
CANCELLED = "Alert cancelled. The coin isn't returned."
NOT_SET = "This alert isn't set any more."
FIRED = (
    "🔔 {nickname} is using RealChat now · /userid_{public_id}\n"
    "This is activity in RealChat, not Telegram online status."
)
REFUND_EXPIRED = (
    "🔔 {nickname} didn't use RealChat within {days}, so your alert ended. {price} returned."
)
REFUND_ENDED = (
    "🔔 Your activity alert for {nickname} can't continue, so it ended. {price} returned."
)
SOMEONE = "this person"


def _now() -> datetime:
    return datetime.now(UTC)


async def _remove_buttons(bot: Bot, query: CallbackQuery) -> None:
    """Best effort: Telegram may refuse, e.g. for an old message."""
    if query.message is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=None
        )
    except TelegramAPIError as exc:
        logger.info("could not remove buttons", extra=exception_summary(exc))


async def _nickname(session: AsyncSession, user_id: int) -> str:
    """The nickname, or a neutral word for an account deleted meanwhile."""
    nickname = (await users.get_profile(session, user_id)).nickname
    return str(nickname) if nickname else SOMEONE


async def on_ask(
    query: CallbackQuery,
    bot: Bot,
    callback_data: AlertAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    other_id = await users.user_id_for(session, callback_data.profile)
    if other_id is None:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    refused = await alerts.refusal(session, user_id, other_id)
    nickname = await _nickname(session, other_id)
    price = await runtime_settings.get(session, SettingKey.ACTIVITY_ALERT_COINS)
    days = await runtime_settings.get(session, SettingKey.ACTIVITY_ALERT_DAYS)
    available = (await coins.balance(session, user_id)).available
    await session.commit()
    if refused is not None:
        await query.answer(REFUSALS[refused].format(nickname=nickname))
        return
    if available < price:
        await query.answer(
            NO_COINS.format(price=coins_label(price), available=coins_label(available)),
            show_alert=True,
        )
        return
    await query.answer()
    buy = AlertAction(
        action="buy", profile=callback_data.profile, draft=secrets.token_hex(8), price=price
    )
    await bot.send_message(
        query.from_user.id,
        CONFIRM.format(nickname=nickname, days=days_label(days), price=coins_label(price)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=BUY_BUTTON, callback_data=buy.pack()),
                    InlineKeyboardButton(
                        text=DISMISS_BUTTON, callback_data=AlertAction(action="dismiss").pack()
                    ),
                ]
            ]
        ),
    )


async def on_dismiss(query: CallbackQuery, bot: Bot) -> None:
    await query.answer(DISMISSED)
    await _remove_buttons(bot, query)


async def on_buy(
    query: CallbackQuery,
    bot: Bot,
    callback_data: AlertAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    key = f"{user_id}:{callback_data.draft}"
    if await alerts.bought_under(session, key):
        await session.commit()
        await query.answer(ALREADY_BOUGHT)
        await _remove_buttons(bot, query)
        return
    other_id = await users.user_id_for(session, callback_data.profile)
    price = await runtime_settings.get(session, SettingKey.ACTIVITY_ALERT_COINS)
    if other_id is None or not callback_data.draft:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    if callback_data.price != price:
        # Never charge anything but the price the user was shown.
        await session.commit()
        await query.answer(PRICE_CHANGED, show_alert=True)
        await _remove_buttons(bot, query)
        return
    bought = await alerts.buy(session, user_id, other_id, key, price, _now())
    nickname = await _nickname(session, other_id)
    await session.commit()
    await _remove_buttons(bot, query)
    if bought.already:
        await query.answer(ALREADY_BOUGHT)
        return
    if bought.alert_id is None:
        refused = bought.refusal or Refusal.UNAVAILABLE
        await query.answer(REFUSALS[refused].format(nickname=nickname))
        return
    await query.answer()
    await bot.send_message(
        query.from_user.id, BOUGHT.format(nickname=nickname, price=coins_label(price))
    )


async def on_cancel(
    query: CallbackQuery,
    bot: Bot,
    callback_data: AlertAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    cancelled = await alerts.cancel(session, callback_data.alert, user_id, _now())
    await session.commit()
    await query.answer(CANCELLED if cancelled else NOT_SET)


class AlertNotices:
    """Tells buyers that an alert fired or was refunded. Used by the worker."""

    def __init__(
        self,
        bot: Bot,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._runtime = runtime_settings

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except TelegramAPIError as exc:
            logger.info("could not send an activity alert notice", extra=exception_summary(exc))

    async def fired(self, alert: Settled) -> None:
        async with self._sessions() as session:
            chat_id = (await users.get_user(session, alert.buyer_id)).telegram_user_id
            profile = await users.get_profile(session, alert.target_id)
            await session.commit()
        text = FIRED.format(nickname=profile.nickname or SOMEONE, public_id=profile.public_id)
        await self._send(chat_id, text)

    async def refunded(self, alert: Settled) -> None:
        async with self._sessions() as session:
            chat_id = (await users.get_user(session, alert.buyer_id)).telegram_user_id
            nickname = await _nickname(session, alert.target_id)
            days = await self._runtime.get(session, SettingKey.ACTIVITY_ALERT_DAYS)
            await session.commit()
        price = coins_label(alert.price)
        # Only an expiry gives its reason: a block or an opt-out is never revealed.
        text = (
            REFUND_EXPIRED.format(nickname=nickname, days=days_label(days), price=price)
            if alert.reason == "expired"
            else REFUND_ENDED.format(nickname=nickname, price=price)
        )
        await self._send(chat_id, text)


def create_router() -> Router:
    router = Router(name="activity_alerts")
    # Private chats only: alerts are never set or shown in a group.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_ask, AlertAction.filter(F.action == "ask"))
    router.callback_query.register(on_dismiss, AlertAction.filter(F.action == "dismiss"))
    router.callback_query.register(on_buy, AlertAction.filter(F.action == "buy"))
    router.callback_query.register(on_cancel, AlertAction.filter(F.action == "cancel"))
    return router
