"""🔒 Private mode in a chat (D-031, D-047).

Either user turns it on or off from the chat keyboard, and both are told. Turning it
off queues the session's messages for deletion (see `backend.modules.chat.private`);
the worker ends it when the chat ends or the time limit is reached, and
`TelegramDeletions` deletes the queued messages from both users' Telegram chats.

The note shown when it is turned on says plainly what it does and does not do: screen
capture, saving, and forwarding are blocked only in Telegram's own apps (all relayed
messages are protected content, D-036), it is not a Telegram secret chat, not
end-to-end encrypted, and RealChat still keeps a copy for moderation.

As elsewhere, every database transaction is ended before any Telegram call.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message, ReplyKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.keyboards import PRIVATE_OFF, PRIVATE_ON, chat_keyboard, main_keyboard
from backend.core.logging import exception_summary
from backend.modules.chat import private
from backend.modules.chat import service as chat
from backend.modules.chat.private import Due, SessionEnded
from backend.modules.users import service as users
from backend.modules.users.models import User

logger = logging.getLogger(__name__)

NOTE = (
    "Messages sent from now on are deleted from both chats when Private mode is turned "
    "off or the chat ends. Screenshots, screen recording, saving and forwarding are "
    "blocked in Telegram's own apps; nothing can stop someone photographing the screen "
    "with another device. RealChat still keeps a copy for moderation for a limited time. "
    "This isn't a Telegram secret chat and it isn't end-to-end encrypted."
)
YOU_TURNED_ON = f"🔒 You turned on Private mode.\n\n{NOTE}"
PARTNER_TURNED_ON = f"🔒 Your chat partner turned on Private mode.\n\n{NOTE}"
ALREADY_ON = "🔒 Private mode is already on."
NOT_ON = "Private mode isn't on."
NOT_IN_CHAT = "You're not in a chat right now."
DELETING = "Messages sent in Private mode are being deleted from both chats."
DICE_LATER = (
    "Telegram doesn't let dice and games be deleted until they're a day old, so those "
    "will be removed automatically within a day."
)
YOU_TURNED_OFF = f"🔓 You turned off Private mode. {DELETING}"
PARTNER_TURNED_OFF = f"🔓 Your chat partner turned off Private mode. {DELETING}"
TIME_LIMIT = (
    f"🔓 Private mode ended because of its time limit. {DELETING} You can turn it on again."
)
CHAT_ENDED = "🔓 Messages sent in Private mode in your last chat are being deleted from both chats."


def _with_dice(text: str, has_dice: bool) -> str:
    return f"{text}\n\n{DICE_LATER}" if has_dice else text


async def _telegram_id(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(select(User.telegram_user_id).where(User.id == user_id))
    ).scalar_one()


async def _tell(bot: Bot, chat_id: int, text: str, keyboard: ReplyKeyboardMarkup | None) -> None:
    """Tell a user, who may have blocked the bot: never raises."""
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
    except TelegramAPIError as exc:
        logger.warning("could not send a Private mode notice", extra=exception_summary(exc))


async def _toggle(message: Message, bot: Bot, session: AsyncSession, on: bool) -> None:
    if message.from_user is None:
        return
    user = await users.get_or_create_user(session, message.from_user.id)
    active = await chat.active_chat(session, user.id)
    now = datetime.now(UTC)
    toggled = (
        None
        if active is None
        else await (private.turn_on if on else private.turn_off)(
            session, active.conversation_id, user.id, now
        )
    )
    if active is None or toggled is None:
        await session.commit()
        await message.answer(NOT_IN_CHAT, reply_markup=main_keyboard())
        return
    partner_telegram_id = await _telegram_id(session, active.partner_id)
    await session.commit()
    keyboard = chat_keyboard(private=on)
    if not toggled.changed:
        await message.answer(ALREADY_ON if on else NOT_ON, reply_markup=keyboard)
        return
    mine, theirs = (
        (YOU_TURNED_ON, PARTNER_TURNED_ON) if on else (YOU_TURNED_OFF, PARTNER_TURNED_OFF)
    )
    # The partner is told first, so a failure replying to this user cannot skip it.
    await _tell(bot, partner_telegram_id, _with_dice(theirs, toggled.has_dice), keyboard)
    await message.answer(_with_dice(mine, toggled.has_dice), reply_markup=keyboard)


async def on_private_on(message: Message, bot: Bot, session: AsyncSession) -> None:
    await _toggle(message, bot, session, on=True)


async def on_private_off(message: Message, bot: Bot, session: AsyncSession) -> None:
    await _toggle(message, bot, session, on=False)


class PrivateModeNotifier:
    """Tells both users that the worker ended Private mode. Used by the worker."""

    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions

    async def private_ended(self, ended: SessionEnded) -> None:
        async with self._sessions() as session:
            telegram_ids = [await _telegram_id(session, user_id) for user_id in ended.user_ids]
            await session.commit()
        if ended.reason == "time_limit":
            # Still in the chat: the keyboard shows Private mode as off again.
            text, keyboard = TIME_LIMIT, chat_keyboard(private=False)
        else:
            text, keyboard = CHAT_ENDED, None
        for telegram_id in telegram_ids:
            await _tell(self._bot, telegram_id, _with_dice(text, ended.has_dice), keyboard)


# --- Deleting the queued messages ------------------------------------------------------

DELETE_BATCH = 500
# Telegram's limit on message ids per deleteMessages call.
PER_CALL = 100


class TelegramDeletions:
    """Deletes queued messages from users' Telegram chats. Runs in the worker.

    Each tick claims a bounded batch, deletes up to 100 messages per chat per call, and
    removes each entry once the message is deleted or can't be deleted (already gone,
    or the chat no longer reachable). Entries Telegram could not be asked about (a
    network error, or a request to slow down) stay queued and are retried. Only counts
    are logged.
    """

    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions

    async def tick(self, now: datetime) -> int:
        """Process one batch; returns how many entries were settled."""
        async with self._sessions() as session:
            expired = await private.drop_expired(session, now)
            due = await private.claim_due(session, now, DELETE_BATCH)
            await session.commit()
        if expired:
            logger.warning("deletions passed Telegram's limit", extra={"count": expired})
        by_chat: dict[int, list[Due]] = defaultdict(list)
        for entry in due:
            by_chat[entry.chat_id].append(entry)
        settled: list[int] = []
        try:
            for chat_id, entries in by_chat.items():
                for start in range(0, len(entries), PER_CALL):
                    settled += await self._delete(chat_id, entries[start : start + PER_CALL])
        except TelegramRetryAfter:
            # Asked to slow down: what was not tried stays queued for a later tick.
            logger.info("deletions paused at Telegram's request")
        async with self._sessions() as session:
            await private.remove(session, settled)
            await session.commit()
        if settled:
            logger.info("deleted queued messages", extra={"count": len(settled)})
        return len(settled)

    async def _delete(self, chat_id: int, entries: list[Due]) -> list[int]:
        """Delete these messages from one chat; returns the entries that are settled."""
        try:
            await self._bot.delete_messages(chat_id, [e.message_id for e in entries])
            return [e.id for e in entries]
        except TelegramForbiddenError:
            return [e.id for e in entries]  # the user blocked the bot or is gone
        except TelegramBadRequest:
            pass  # one of them can't be deleted: try each on its own
        except TelegramRetryAfter:
            raise
        except TelegramAPIError as exc:
            logger.warning("could not delete messages", extra=exception_summary(exc))
            return []
        settled = []
        for entry in entries:
            try:
                await self._bot.delete_message(chat_id, entry.message_id)
            except TelegramRetryAfter:
                raise
            except (TelegramBadRequest, TelegramForbiddenError):
                pass  # already gone, or can't be deleted: nothing more to try
            except TelegramAPIError as exc:
                logger.warning("could not delete a message", extra=exception_summary(exc))
                continue
            settled.append(entry.id)
        return settled


def create_router() -> Router:
    router = Router(name="private")
    # Private chats only: Private mode belongs to one-to-one chats with the bot.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_private_on, F.text == PRIVATE_ON)
    router.message.register(on_private_off, F.text == PRIVATE_OFF)
    return router
