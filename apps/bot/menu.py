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
from backend.modules.users.terms import terms_parts


class HelpAction(CallbackData, prefix="help"):
    action: str


# Shown in Telegram's menu next to the message box, and listed in /help.
COMMANDS = (
    ("start", "Set up your account, or continue where you left off"),
    ("profile", "View and edit your profile"),
    ("coins", "Your coins and recent activity"),
    ("vip", "VIP status and the VIP pass"),
    ("userid", "Open a profile by its RealChat ID, or see yours"),
    ("help", "How RealChat works, terms, and contacts"),
    ("terms", "Terms of use and privacy notice"),
    ("paysupport", "Help with a purchase"),
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


async def send_parts(
    bot: Bot, chat_id: int, parts: list[str], reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Send a text split into several messages (`split_message`), in order; any buttons
    go on the last one."""
    for part in parts[:-1]:
        await bot.send_message(chat_id, part)
    await bot.send_message(chat_id, parts[-1], reply_markup=reply_markup)


TERMS_WEB_BUTTON = "📄 Terms on the web"
PRIVACY_WEB_BUTTON = "🔒 Privacy on the web"


def web_links(settings: BotSettings) -> InlineKeyboardMarkup | None:
    """Links to the terms and privacy web pages (D-069), once their address is set."""
    if settings.public_base_url is None:
        return None
    base = str(settings.public_base_url).rstrip("/")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TERMS_WEB_BUTTON, url=f"{base}/terms")],
            [InlineKeyboardButton(text=PRIVACY_WEB_BUTTON, url=f"{base}/privacy")],
        ]
    )


async def send_terms(bot: Bot, chat_id: int, settings: BotSettings) -> None:
    """The terms of use and privacy notice for reading again, with the web links."""
    await send_parts(bot, chat_id, terms_parts(settings.min_age), web_links(settings))


async def show_terms(query: CallbackQuery, bot: Bot, settings: BotSettings) -> None:
    await query.answer()
    await send_terms(bot, query.from_user.id, settings)


def create_router() -> Router:
    router = Router(name="menu")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.message.register(show_help, Command("help"))
    router.callback_query.register(show_terms, HelpAction.filter(F.action == "terms"))
    return router
