"""❤️ Like and 💔 Unlike on other users' cards (D-056).

Every other user's card has Like above Block and Report; once liked, the button turns
into Unlike on the same card. The button carries the person's RealChat ID, and the
service checks again on every tap that both accounts can still use RealChat and that
the user hasn't blocked them, so an old or forged button likes nobody it shouldn't.
Repeated taps never like twice.

A new like tells the liked user, with a button to the liker's card, unless they turned
like alerts off or were already told about this pair's like within the gap (an admin
setting). A like from the current chat partner says so instead of naming them.
Unliking is silent. Likes are free. The lists are in People (`apps.bot.people`). As
elsewhere, every database transaction is ended before any Telegram call.
"""

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.endings import LikeAction, is_like_row, like_row
from apps.bot.people import NOT_AVAILABLE, VIEW_PROFILE_BUTTON, PeopleAction, ready_user
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.people import likes
from backend.modules.people.likes import LikeOutcome
from backend.modules.users import service as users
from backend.modules.users.service import Notification

logger = logging.getLogger(__name__)

LIKED = "❤️ Liked."
ALREADY_LIKED = "Already liked."
UNLIKED = "Unliked."
NOT_LIKED = "Not liked."
UNBLOCK_TO_LIKE = "You blocked this person. Unblock them to like them."
LIKE_NOTICE = "❤️ {nickname} liked your profile."
PARTNER_LIKE_NOTICE = "❤️ Your chat partner liked your profile."


class LikeNotice:
    """A like notice to send once the transaction is committed."""

    def __init__(self, chat_id: int, liker_id: int, nickname: str, partner: bool) -> None:
        self.chat_id, self.liker_id, self.nickname = chat_id, liker_id, nickname
        self.partner = partner  # the liker is the liked user's current chat partner

    async def send(self, bot: Bot) -> None:
        """Best effort: the like stands even if the user can't be reached."""
        if self.partner:
            # They are chatting: the partner's card is one tap away on the chat keyboard.
            text, markup = PARTNER_LIKE_NOTICE, None
        else:
            text = LIKE_NOTICE.format(nickname=self.nickname)
            button = InlineKeyboardButton(
                text=VIEW_PROFILE_BUTTON,
                callback_data=PeopleAction(action="liker", user=self.liker_id).pack(),
            )
            markup = InlineKeyboardMarkup(inline_keyboard=[[button]])
        try:
            await bot.send_message(self.chat_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            logger.info("could not send a like notice", extra=exception_summary(exc))


async def _notice(session: AsyncSession, liker_id: int, liked_id: int) -> LikeNotice:
    active = await chat.active_chat(session, liker_id)
    partner = active is not None and active.partner_id == liked_id
    nickname = (await users.get_profile(session, liker_id)).nickname
    chat_id = (await users.get_user(session, liked_id)).telegram_user_id
    return LikeNotice(chat_id, liker_id, str(nickname), partner)


async def _set_like_button(bot: Bot, query: CallbackQuery, public_id: str, liked: bool) -> None:
    """Turn the tapped card's Like into Unlike, or back, keeping its other buttons. Best
    effort: Telegram may refuse, e.g. when they are unchanged."""
    message = query.message
    if not isinstance(message, Message) or message.reply_markup is None:
        return
    others = [row for row in message.reply_markup.inline_keyboard if not is_like_row(row)]
    markup = InlineKeyboardMarkup(inline_keyboard=[like_row(public_id, liked), *others])
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=message.message_id, reply_markup=markup
        )
    except TelegramAPIError as exc:
        logger.info("could not update buttons", extra=exception_summary(exc))


async def on_like(
    query: CallbackQuery,
    bot: Bot,
    callback_data: LikeAction,
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
    hours = await runtime_settings.get(session, SettingKey.LIKE_NOTICE_GAP_HOURS)
    alerts = (await users.notification_settings(session, other_id))[Notification.LIKE_ALERTS]
    liked = await likes.like(
        session, user_id, other_id, datetime.now(UTC), timedelta(hours=hours), announce=alerts
    )
    if liked.outcome in (LikeOutcome.UNAVAILABLE, LikeOutcome.BLOCKED):
        await session.commit()
        blocked = liked.outcome == LikeOutcome.BLOCKED
        await query.answer(UNBLOCK_TO_LIKE if blocked else NOT_AVAILABLE)
        return
    notice = await _notice(session, user_id, other_id) if liked.notify else None
    await session.commit()
    await query.answer(LIKED if liked.outcome == LikeOutcome.LIKED else ALREADY_LIKED)
    await _set_like_button(bot, query, callback_data.profile, liked=True)
    if notice is not None:
        await notice.send(bot)


async def on_unlike(
    query: CallbackQuery,
    bot: Bot,
    callback_data: LikeAction,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    user_id = await ready_user(query, bot, session, state, settings)
    if user_id is None:
        return
    other_id = await users.user_id_for(session, callback_data.profile)
    removed = other_id is not None and await likes.unlike(session, user_id, other_id)
    await session.commit()
    await query.answer(UNLIKED if removed else NOT_LIKED)
    if other_id is not None:
        await _set_like_button(bot, query, callback_data.profile, liked=False)


def create_router() -> Router:
    router = Router(name="likes")
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_like, LikeAction.filter(F.action == "like"))
    router.callback_query.register(on_unlike, LikeAction.filter(F.action == "unlike"))
    return router
