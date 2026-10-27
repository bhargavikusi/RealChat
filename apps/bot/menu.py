"""/help and the bot's command menu (D-022, D-023).

Help is available to everyone, including users who have not finished onboarding,
and saves nothing. Links to the announcement channel and support are shown only
when they are configured.
"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from backend.config.settings import BotSettings
from backend.modules.users.terms import terms_body


class HelpAction(CallbackData, prefix="help"):
    action: str


# Shown in Telegram's menu next to the message box, and listed in /help.
COMMANDS = (
    ("start", "Set up your account, or continue where you left off"),
    ("profile", "View and edit your profile"),
    ("help", "How RealChat works, terms, and contacts"),
)
BOT_COMMANDS = [BotCommand(command=name, description=text) for name, text in COMMANDS]

HELP_TEXT = (
    "RealChat help\n\n"
    "RealChat is an anonymous chat on Telegram. Other users see only your nickname and "
    "the profile details you choose to add, never your Telegram name, username, or "
    "phone number. Messages pass through the RealChat bot and are not end-to-end "
    "encrypted.\n\n"
    "Commands (also in the menu next to the message box):\n"
    + "\n".join(f"/{name} - {text}" for name, text in COMMANDS)
)


def help_keyboard(settings: BotSettings) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="📄 Terms and privacy", callback_data=HelpAction(action="terms").pack()
            )
        ]
    ]
    if settings.announcement_channel_url is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📢 Announcements", url=str(settings.announcement_channel_url)
                )
            ]
        )
    if settings.support_contact is not None:
        rows.append([InlineKeyboardButton(text="💬 Support", url=str(settings.support_contact))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_help(message: Message, settings: BotSettings) -> None:
    await message.answer(HELP_TEXT, reply_markup=help_keyboard(settings))


async def show_terms(query: CallbackQuery, bot: Bot, settings: BotSettings) -> None:
    await query.answer()
    await bot.send_message(query.from_user.id, terms_body(settings.min_age))


def create_router() -> Router:
    router = Router(name="menu")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.message.register(show_help, Command("help"))
    router.callback_query.register(show_terms, HelpAction.filter(F.action == "terms"))
    return router
