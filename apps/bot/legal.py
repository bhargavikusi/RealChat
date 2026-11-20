"""The welcome screen and RealChat's User Agreement and Privacy Policy in the bot (D-070).

At first start, and whenever the documents change, the user sees a short welcome with
a button for each document and a blue ✅ Continue keyboard button; tapping Continue
accepts both (handled in `apps.bot.onboarding`). The document buttons open the web
pages once PUBLIC_BASE_URL is set; until then they send the document in the bot.
/terms and Help's Terms button show the same document buttons.

Formatting uses message entities, never a parse mode, so no text can be read as markup.
"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.formatting import Bold, Italic, Text, TextLink
from pydantic import HttpUrl

from backend.config.settings import BotSettings
from backend.modules.users.terms import (
    MESSAGE_LIMIT,
    Block,
    Document,
    document_blocks,
    effective_label,
    text_length,
)


class LegalAction(CallbackData, prefix="legal"):
    """Read a document in the bot; restricted users may too (the access gate allows it)."""

    document: str  # a `Document` value


CONTINUE_BUTTON = "✅ Continue"
CONTINUE_HINT = "Tap ✅ Continue below to start."
BUTTON_LABELS = {
    Document.PRIVACY_POLICY: "🔒 Privacy Policy",
    Document.USER_AGREEMENT: "📜 User Agreement",
}
LINK_TEXTS = {Document.PRIVACY_POLICY: "Privacy Policy", Document.USER_AGREEMENT: "User Agreement"}
WELCOME_TITLE = "👋 Welcome to RealChat"
WELCOME_TAGLINE = "Real people. Real conversations."
WELCOME_ABOUT = (
    "🎭 Anonymous 1-on-1 chats with real people. Your Telegram name, username, and "
    "phone number stay private."
)
UPDATED_TITLE = "📜 We've updated our Privacy Policy and User Agreement"
UPDATED_ABOUT = "Please read what's changed before you continue."
DOCUMENTS_TITLE = "📜 RealChat's Privacy Policy and User Agreement"
DOCUMENTS_ABOUT = "Tap a button below to read them."


def _url(public_base_url: HttpUrl | None, document: Document) -> str | None:
    if public_base_url is None:
        return None
    base = str(public_base_url).rstrip("/")
    return f"{base}/{'privacy' if document is Document.PRIVACY_POLICY else 'terms'}"


def _name(public_base_url: HttpUrl | None, document: Document) -> Text:
    """A document's name, linked to its page once the address is set."""
    url = _url(public_base_url, document)
    name = LINK_TEXTS[document]
    return Text(Bold(name)) if url is None else Text(TextLink(name, url=url))


def documents_keyboard(public_base_url: HttpUrl | None) -> InlineKeyboardMarkup:
    """One button per document: a link to its page once the address is set, otherwise a
    button that sends it in the bot."""
    row = []
    for document in (Document.PRIVACY_POLICY, Document.USER_AGREEMENT):
        url = _url(public_base_url, document)
        label = BUTTON_LABELS[document]
        row.append(
            InlineKeyboardButton(text=label, url=url)
            if url is not None
            else InlineKeyboardButton(
                text=label, callback_data=LegalAction(document=document.value).pack()
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def continue_keyboard() -> ReplyKeyboardMarkup:
    """The blue ✅ Continue button (`primary` style; older apps show their usual colour)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CONTINUE_BUTTON, style="primary")]],
        resize_keyboard=True,
    )


def welcome(public_base_url: HttpUrl | None, *, updated: bool) -> Text:
    """The welcome at first start, or the notice that the documents changed."""
    title, about = (UPDATED_TITLE, UPDATED_ABOUT) if updated else (WELCOME_TITLE, WELCOME_ABOUT)
    lines: list[Text | str] = [Bold(title), "\n"]
    if not updated:
        lines += [Italic(WELCOME_TAGLINE), "\n"]
    return Text(
        *lines,
        "\n",
        about,
        "\n\n",
        "By tapping ",
        Bold(CONTINUE_BUTTON),
        " you confirm your agreement with our ",
        _name(public_base_url, Document.PRIVACY_POLICY),
        " and ",
        _name(public_base_url, Document.USER_AGREEMENT),
        ".",
    )


def welcome_text(*, updated: bool) -> str:
    """The welcome's plain text, which doesn't depend on whether its names are links."""
    return str(welcome(None, updated=updated).as_kwargs()["text"])


def documents_summary() -> Text:
    """What /terms and Help's Terms button show above the document buttons."""
    return Text(Bold(DOCUMENTS_TITLE), "\n", effective_label(), "\n\n", DOCUMENTS_ABOUT)


async def send_welcome(bot: Bot, chat_id: int, settings: BotSettings, *, updated: bool) -> None:
    """The welcome with its document buttons, then the Continue keyboard (a message can
    carry only one kind of keyboard)."""
    await bot.send_message(
        chat_id,
        **welcome(settings.public_base_url, updated=updated).as_kwargs(),
        reply_markup=documents_keyboard(settings.public_base_url),
    )
    await bot.send_message(chat_id, CONTINUE_HINT, reply_markup=continue_keyboard())


async def send_documents(bot: Bot, chat_id: int, settings: BotSettings) -> None:
    await bot.send_message(
        chat_id,
        **documents_summary().as_kwargs(),
        reply_markup=documents_keyboard(settings.public_base_url),
    )


def _render(block: Block) -> list[Text | str]:
    """A block as message text: headings bold, list items bulleted."""
    if block.kind == "title":
        return [Bold(block.text), "\n", Italic(effective_label())]
    if block.kind in ("section", "subsection"):
        return [Bold(block.text)]
    if block.kind == "item":
        return [f"• {block.text}"]
    return [block.text]


def document_messages(document: Document, minimum_age: int) -> list[Text]:
    """A document as the messages it is sent in: blocks in order, as many per message as
    fit within Telegram's limit."""
    messages: list[Text] = []
    current: list[Text | str] = []
    for block in document_blocks(document, minimum_age):
        rendered = _render(block)
        gap = "\n" if block.kind == "item" else "\n\n"
        separator: list[Text | str] = [gap] if current else []
        candidate = [*current, *separator, *rendered]
        if current and text_length(Text(*candidate).as_kwargs()["text"]) > MESSAGE_LIMIT:
            messages.append(Text(*current))
            current = rendered
        else:
            current = candidate
    messages.append(Text(*current))
    return messages


async def send_document(bot: Bot, chat_id: int, document: Document, minimum_age: int) -> None:
    for message in document_messages(document, minimum_age):
        await bot.send_message(chat_id, **message.as_kwargs())


async def on_read(
    query: CallbackQuery, bot: Bot, callback_data: LegalAction, settings: BotSettings
) -> None:
    """Send a document in the bot (only offered while PUBLIC_BASE_URL is empty)."""
    try:
        document = Document(callback_data.document)
    except ValueError:
        await query.answer()
        return
    await query.answer()
    await send_document(bot, query.from_user.id, document, settings.min_age)


def create_router() -> Router:
    router = Router(name="legal")
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(on_read, LegalAction.filter())
    return router
