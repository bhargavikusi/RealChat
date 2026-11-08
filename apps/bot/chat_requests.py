"""Chat requests: 💬 Chat request on a profile card (D-058).

1. 💬 Chat request on another user's card shows the price and that it is taken once the
   request is delivered and never returned, with Send and Cancel. Whether the person
   can take a request is only checked on the tap, so a card never shows whether
   someone is active, chatting, or in Silent mode; a refusal gives no reason.
2. Send holds the price and records the request, then sends the recipient a notice
   with Accept and Decline. Once Telegram accepts the notice the price is taken; if it
   fails, the hold is released and the sender is told nothing was taken.
3. Decline tells the sender. Accept asks a recipient who is chatting or searching to
   confirm first. A free sender is connected at once; a sender who is chatting or
   searching is asked to Switch or Stay, and the request lapses if they stay or don't
   answer in time. Connecting ends any chat either is in (their partner is told, as
   for any ending) and their searches (a paid search's held coins are returned).

The worker expires unanswered requests, lapses unanswered switches, and fails sends
that were interrupted (`RequestNotices` tells the users). The wording and layout are
provisional until the design pass. As elsewhere, every database transaction is ended
before any Telegram call.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.avatars import Avatars
from apps.bot.cards import send_card
from apps.bot.chat import COINS_RETURNED, duration, release_search_hold, tell_partner_ended
from apps.bot.endings import RequestAction, coins_label
from apps.bot.keyboards import chat_keyboard
from apps.bot.people import NOT_AVAILABLE, ready_user
from apps.bot.relay import unreachable
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.chat_requests import service as requests
from backend.modules.chat_requests.service import ConnectOutcome, Parties, Refusal
from backend.modules.coins import service as coins
from backend.modules.matching.queue import SearchQueue, parse_search
from backend.modules.users import service as users
from backend.modules.users.terms import TERMS_VERSION

logger = logging.getLogger(__name__)

CONFIRM = (
    "💬 Send {nickname} a chat request for {price}? It's taken once the request is "
    "delivered, and isn't returned if it's declined or expires."
)
SEND_BUTTON = "💬 Send request"
CANCEL_BUTTON = "Cancel"
CANCELLED = "Cancelled."
NOT_NOW = "{nickname} can't take a chat request right now."
REFUSALS = {
    Refusal.YOU_BLOCKED: "You blocked this person. Unblock them to send a chat request.",
    Refusal.BUSY: "End your chat or search first.",
    Refusal.WAITING: "Your chat request to {nickname} is still waiting for an answer.",
    Refusal.NO_COINS: "You don't have enough coins to send this request.",
}
NO_COINS = "You need {price} to send a chat request, and you have {available}."
PRICE_CHANGED = "The price has changed. Please tap 💬 Chat request again."
ALREADY_SENT = "Already sent."
REQUEST_NOTICE = (
    "💬 {nickname} · /userid_{public_id} wants to chat with you. Answer within {expiry}."
)
ACCEPT_BUTTON = "✅ Accept"
DECLINE_BUTTON = "Decline"
SENT = "💬 Chat request sent to {nickname}. {price} used. You'll be told when they answer."
SENT_FREE = "💬 Chat request sent to {nickname}. You'll be told when they answer."
NOT_DELIVERED = "Your chat request couldn't be delivered to {nickname}. No coins were taken."
GONE = "This chat request isn't available any more."
DECLINED = "Declined."
DECLINED_NOTICE = "{nickname} declined your chat request."
EXPIRED_NOTICE = "Your chat request to {nickname} expired."
CONFIRM_BUSY = "Accepting ends your current {what}. Continue?"
END_AND_ACCEPT = "✅ End and accept"
SWITCH_PROMPT = (
    "💬 {nickname} accepted your chat request. Switch to them? Your current {what} will "
    "end. Answer within {window}."
)
SWITCH_BUTTON = "🔀 Switch"
STAY_BUTTON = "Stay"
WAITING_FOR = "⏳ Waiting for {nickname}…"
STAYED = "You stayed."
LAPSED_NOTICE = "{nickname} can't chat right now."
CONNECTED = "💬 You're connected! Say hi."
RACED = "Couldn't connect: one of you has just started another chat."
UNAVAILABLE_NOW = "{nickname} isn't available any more."


def _now() -> datetime:
    return datetime.now(UTC)


def _buttons(*rows: tuple[str, RequestAction]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data.pack()) for text, data in rows]
        ]
    )


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


async def _tell(
    bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Best effort: the request's state stands even if the user can't be reached."""
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramAPIError as exc:
        logger.info("could not send a chat request update", extra=exception_summary(exc))


async def _nickname(session: AsyncSession, user_id: int) -> str:
    return str((await users.get_profile(session, user_id)).nickname)


async def _chat_id(session: AsyncSession, user_id: int) -> int:
    return (await users.get_user(session, user_id)).telegram_user_id


async def _busy(session: AsyncSession, search_queue: SearchQueue, user_id: int) -> str | None:
    """What the user is doing that a connection would end: "chat", "search", or None."""
    if await chat.active_chat(session, user_id) is not None:
        return "chat"
    return "search" if await search_queue.current_search(user_id) is not None else None


async def _active_since(session: AsyncSession, runtime_settings: RuntimeSettings) -> datetime:
    minutes = await runtime_settings.get(session, SettingKey.CHAT_REQUEST_ACTIVE_MINUTES)
    return _now() - timedelta(minutes=minutes)


async def _expiry(session: AsyncSession, runtime_settings: RuntimeSettings) -> int:
    return await runtime_settings.get(session, SettingKey.CHAT_REQUEST_EXPIRY_MINUTES)


# --- Sending ---------------------------------------------------------------------------


async def on_ask(
    query: CallbackQuery,
    bot: Bot,
    callback_data: RequestAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    search_queue: SearchQueue,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    other_id = await users.user_id_for(session, callback_data.profile)
    if other_id is None:
        await session.commit()
        await query.answer(NOT_AVAILABLE)
        return
    searching = await search_queue.current_search(user_id) is not None
    since = await _active_since(session, runtime_settings)
    refused = await requests.refusal(session, user_id, other_id, since, sender_searching=searching)
    nickname = await _nickname(session, other_id)
    price = await runtime_settings.get(session, SettingKey.CHAT_REQUEST_COINS)
    available = (await coins.balance(session, user_id)).available
    await session.commit()
    if refused is not None:
        text = NOT_NOW if refused == Refusal.UNAVAILABLE else REFUSALS[refused]
        await query.answer(text.format(nickname=nickname))
        return
    if available < price:
        await query.answer(
            NO_COINS.format(price=coins_label(price), available=coins_label(available)),
            show_alert=True,
        )
        return
    await query.answer()
    send = RequestAction(
        action="send", profile=callback_data.profile, draft=secrets.token_hex(8), price=price
    )
    await bot.send_message(
        query.from_user.id,
        CONFIRM.format(nickname=nickname, price=coins_label(price)),
        reply_markup=_buttons((SEND_BUTTON, send), (CANCEL_BUTTON, RequestAction(action="cancel"))),
    )


async def on_cancel(query: CallbackQuery, bot: Bot) -> None:
    await query.answer(CANCELLED)
    await _remove_buttons(bot, query)


async def on_send(
    query: CallbackQuery,
    bot: Bot,
    callback_data: RequestAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    search_queue: SearchQueue,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    key = f"{user_id}:{callback_data.draft}"
    if await requests.sent_under(session, key):
        await session.commit()
        await query.answer(ALREADY_SENT)
        await _remove_buttons(bot, query)
        return
    other_id = await users.user_id_for(session, callback_data.profile)
    price = await runtime_settings.get(session, SettingKey.CHAT_REQUEST_COINS)
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
    searching = await search_queue.current_search(user_id) is not None
    since = await _active_since(session, runtime_settings)
    sent = await requests.send(
        session, user_id, other_id, key, price, since, _now(), sender_searching=searching
    )
    nickname = await _nickname(session, other_id)
    if sent.request_id is None or sent.already:
        await session.commit()
        if sent.already:
            await query.answer(ALREADY_SENT)
        else:
            refused = sent.refusal or Refusal.UNAVAILABLE
            text = NOT_NOW if refused == Refusal.UNAVAILABLE else REFUSALS[refused]
            await query.answer(text.format(nickname=nickname))
        await _remove_buttons(bot, query)
        return
    sender = await users.get_profile(session, user_id)
    recipient_chat = await _chat_id(session, other_id)
    expiry = await _expiry(session, runtime_settings)
    await session.commit()
    await query.answer()
    await _remove_buttons(bot, query)
    notice = REQUEST_NOTICE.format(
        nickname=sender.nickname, public_id=sender.public_id, expiry=duration(expiry * 60)
    )
    answer = RequestAction(action="accept", request=sent.request_id)
    decline = RequestAction(action="decline", request=sent.request_id)
    try:
        await bot.send_message(
            recipient_chat,
            notice,
            reply_markup=_buttons((ACCEPT_BUTTON, answer), (DECLINE_BUTTON, decline)),
        )
    except TelegramAPIError as exc:
        logger.info("could not deliver a chat request", extra=exception_summary(exc))
        reason = "unreachable" if unreachable(exc) else "error"
        await requests.failed(session, sent.request_id, reason, _now())
        await session.commit()
        await _tell(bot, query.from_user.id, NOT_DELIVERED.format(nickname=nickname))
        return
    charged = await requests.delivered(session, sent.request_id, _now())
    await session.commit()
    text = (
        SENT.format(nickname=nickname, price=coins_label(price))
        if charged
        else (SENT_FREE.format(nickname=nickname))
    )
    await _tell(bot, query.from_user.id, text)


# --- Answering -------------------------------------------------------------------------


async def on_decline(
    query: CallbackQuery,
    bot: Bot,
    callback_data: RequestAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    since = _now() - timedelta(minutes=await _expiry(session, runtime_settings))
    declined = await requests.decline(session, callback_data.request, user_id, since, _now())
    if declined is None:
        await session.commit()
        await query.answer(GONE)
        await _remove_buttons(bot, query)
        return
    sender_chat = await _chat_id(session, declined.sender_id)
    nickname = await _nickname(session, user_id)
    await session.commit()
    await query.answer(DECLINED)
    await _remove_buttons(bot, query)
    await _tell(bot, sender_chat, DECLINED_NOTICE.format(nickname=nickname))


async def on_accept(
    query: CallbackQuery,
    bot: Bot,
    callback_data: RequestAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    search_queue: SearchQueue,
    avatars: Avatars,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    since = _now() - timedelta(minutes=await _expiry(session, runtime_settings))
    parties = await requests.answerable(session, callback_data.request, user_id, since)
    if parties is None:
        await session.commit()
        await query.answer(GONE)
        await _remove_buttons(bot, query)
        return
    busy = await _busy(session, search_queue, user_id)
    if busy is not None and not callback_data.confirm:
        await session.commit()
        await query.answer()
        confirm = RequestAction(action="accept", request=parties.request_id, confirm=1)
        await bot.send_message(
            query.from_user.id,
            CONFIRM_BUSY.format(what=busy),
            reply_markup=_buttons(
                (END_AND_ACCEPT, confirm), (CANCEL_BUTTON, RequestAction(action="cancel"))
            ),
        )
        return
    sender_busy = await _busy(session, search_queue, parties.sender_id)
    if sender_busy is None:
        await _connect(query, bot, session, search_queue, avatars, parties, "delivered", since)
        return
    accepted = await requests.mark_accepted(session, parties.request_id, user_id, since, _now())
    if accepted is None:
        await session.commit()
        await query.answer(GONE)
        await _remove_buttons(bot, query)
        return
    window = await runtime_settings.get(session, SettingKey.CHAT_REQUEST_SWITCH_SECONDS)
    sender_chat = await _chat_id(session, parties.sender_id)
    sender_nickname = await _nickname(session, parties.sender_id)
    nickname = await _nickname(session, user_id)
    await session.commit()
    await query.answer()
    await _remove_buttons(bot, query)
    await _tell(bot, query.from_user.id, WAITING_FOR.format(nickname=sender_nickname))
    switch = RequestAction(action="switch", request=parties.request_id)
    stay = RequestAction(action="stay", request=parties.request_id)
    await _tell(
        bot,
        sender_chat,
        SWITCH_PROMPT.format(nickname=nickname, what=sender_busy, window=duration(window)),
        _buttons((SWITCH_BUTTON, switch), (STAY_BUTTON, stay)),
    )


async def on_switch(
    query: CallbackQuery,
    bot: Bot,
    callback_data: RequestAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    runtime_settings: RuntimeSettings,
    search_queue: SearchQueue,
    avatars: Avatars,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    window = await runtime_settings.get(session, SettingKey.CHAT_REQUEST_SWITCH_SECONDS)
    since = _now() - timedelta(seconds=window)
    parties = await requests.switchable(session, callback_data.request, user_id, since)
    if parties is None:
        await session.commit()
        await query.answer(GONE)
        await _remove_buttons(bot, query)
        return
    await _connect(query, bot, session, search_queue, avatars, parties, "accepted", since)


async def on_stay(
    query: CallbackQuery,
    bot: Bot,
    callback_data: RequestAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    stayed = await requests.stay(session, callback_data.request, user_id, _now())
    if stayed is None:
        await session.commit()
        await query.answer(GONE)
        await _remove_buttons(bot, query)
        return
    recipient_chat = await _chat_id(session, stayed.recipient_id)
    nickname = await _nickname(session, user_id)
    await session.commit()
    await query.answer(STAYED)
    await _remove_buttons(bot, query)
    await _tell(bot, recipient_chat, LAPSED_NOTICE.format(nickname=nickname))


# --- Connecting ------------------------------------------------------------------------


async def _end_search(session: AsyncSession, search_queue: SearchQueue, user_id: int) -> bool:
    """End the user's search, if any, returning a paid search's held coins. Returns
    whether coins were returned. A search the matcher holds right now is left to it: it
    finds the user in a chat, ends the search, and returns the coins itself."""
    current = await search_queue.current_search(user_id)
    if current is None or not await search_queue.cancel(user_id, current):
        return False
    search = parse_search(current)
    return search.wants is not None and await release_search_hold(session, user_id, search.token)


async def _connect(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    search_queue: SearchQueue,
    avatars: Avatars,
    parties: Parties,
    from_status: str,
    since: datetime,
) -> None:
    """Connect the two users of `parties`, then tell everyone involved."""
    connected = await requests.connect(
        session, parties.request_id, from_status, since, _now(), TERMS_VERSION
    )
    if connected.outcome == ConnectOutcome.RACED:
        await session.rollback()
        await query.answer(RACED, show_alert=True)
        return
    if connected.outcome == ConnectOutcome.GONE:
        await session.commit()
        await query.answer(GONE)
        await _remove_buttons(bot, query)
        return
    actor_is_sender = await _chat_id(session, parties.sender_id) == query.from_user.id
    other_id = parties.recipient_id if actor_is_sender else parties.sender_id
    if connected.outcome == ConnectOutcome.UNAVAILABLE:
        nickname = await _nickname(session, other_id)
        await session.commit()
        await query.answer(UNAVAILABLE_NOW.format(nickname=nickname), show_alert=True)
        await _remove_buttons(bot, query)
        return
    people = {
        user_id: (await _chat_id(session, user_id), await users.get_profile(session, user_id))
        for user_id in (parties.sender_id, parties.recipient_id)
    }
    await session.commit()
    await query.answer()
    await _remove_buttons(bot, query)
    # The chats that were ended for this: each partner left behind is told, as usual.
    for user_id, ended in connected.ended:
        await tell_partner_ended(session, bot, user_id, ended)
    for user_id, partner_id in (
        (parties.sender_id, parties.recipient_id),
        (parties.recipient_id, parties.sender_id),
    ):
        refunded = await _end_search(session, search_queue, user_id)
        header = f"{CONNECTED}\n{COINS_RETURNED}" if refunded else CONNECTED
        telegram_id, _ = people[user_id]
        _, partner = people[partner_id]
        try:
            await send_card(bot, avatars, telegram_id, partner, header, chat_keyboard())
        except TelegramAPIError as exc:
            logger.warning("could not announce a chat request", extra=exception_summary(exc))


# --- From the worker -------------------------------------------------------------------


class RequestNotices:
    """Tells users about requests the worker settled. Used by the worker."""

    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions

    async def _names(self, user_ids: tuple[int, int]) -> tuple[int, str]:
        """The first user's chat id and the second user's nickname."""
        async with self._sessions() as session:
            chat_id = await _chat_id(session, user_ids[0])
            nickname = await _nickname(session, user_ids[1])
            await session.commit()
        return chat_id, nickname

    async def expired(self, parties: Parties) -> None:
        chat_id, nickname = await self._names((parties.sender_id, parties.recipient_id))
        await _tell(self._bot, chat_id, EXPIRED_NOTICE.format(nickname=nickname))

    async def lapsed(self, parties: Parties) -> None:
        chat_id, nickname = await self._names((parties.recipient_id, parties.sender_id))
        await _tell(self._bot, chat_id, LAPSED_NOTICE.format(nickname=nickname))


def create_router() -> Router:
    router = Router(name="chat_requests")
    # Private chats only: requests are never made or answered in a group.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_ask, RequestAction.filter(F.action == "ask"))
    router.callback_query.register(on_cancel, RequestAction.filter(F.action == "cancel"))
    router.callback_query.register(on_send, RequestAction.filter(F.action == "send"))
    router.callback_query.register(on_decline, RequestAction.filter(F.action == "decline"))
    router.callback_query.register(on_accept, RequestAction.filter(F.action == "accept"))
    router.callback_query.register(on_switch, RequestAction.filter(F.action == "switch"))
    router.callback_query.register(on_stay, RequestAction.filter(F.action == "stay"))
    return router
