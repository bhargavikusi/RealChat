"""RealChat's User Agreement and Privacy Policy, and how the bot shows them (D-070)."""

import pytest
from pydantic import HttpUrl

from apps.bot import legal
from backend.modules.users.terms import (
    MESSAGE_LIMIT,
    TERMS_VERSION,
    Document,
    document_blocks,
    document_title,
    effective_label,
    text_length,
)

SECTIONS = {Document.USER_AGREEMENT: 21, Document.PRIVACY_POLICY: 14}


@pytest.mark.parametrize("document", list(Document))
def test_each_document_has_a_title_and_numbered_sections(document: Document) -> None:
    blocks = document_blocks(document, 18)
    assert blocks[0].kind == "title"
    assert [b.kind for b in blocks].count("title") == 1
    sections = [b.text for b in blocks if b.kind == "section"]
    assert len(sections) == SECTIONS[document]
    assert [s.split(".")[0] for s in sections] == [str(n) for n in range(1, len(sections) + 1)]
    # Subsections are numbered within their section.
    section = 0
    for block in blocks:
        if block.kind == "section":
            section = int(block.text.split(".")[0])
        if block.kind == "subsection":
            assert block.text.startswith(f"{section}.")


@pytest.mark.parametrize("document", list(Document))
def test_the_minimum_age_is_filled_in(document: Document) -> None:
    text = " ".join(b.text for b in document_blocks(document, 21))
    assert "{" not in text and "}" not in text
    assert "21 years old" in text


def test_the_documents_state_the_owner_decisions() -> None:
    agreement = " ".join(b.text for b in document_blocks(Document.USER_AGREEMENT, 18))
    privacy = " ".join(b.text for b in document_blocks(Document.PRIVACY_POLICY, 18))
    assert '"RealChat", "we", "us", or "our"' in agreement
    assert "governed by the laws of India" in agreement
    assert "the courts of India have exclusive jurisdiction" in agreement
    assert "Oracle Cloud Infrastructure in India" in privacy
    assert "Digital Personal Data Protection Act, 2023" in privacy
    for text in (agreement, privacy):
        assert "/paysupport" in text
        assert "DRAFT" not in text
        # Blocks are shown as plain text, so the source has no inline Markdown.
        assert "**" not in text and "__" not in text and "`" not in text


def test_titles_and_version_line() -> None:
    assert document_title(Document.USER_AGREEMENT) == "User Agreement"
    assert document_title(Document.PRIVACY_POLICY) == "Privacy Policy"
    assert TERMS_VERSION == "1.1"
    assert effective_label() == "Version 1.1 · Effective 23 November 2026"


def _bold(message: dict[str, object]) -> set[str]:
    text = str(message["text"]).encode("utf-16-le")
    entities = message["entities"]
    assert isinstance(entities, list)
    return {
        text[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")
        for e in entities
        if e.type == "bold"
    }


@pytest.mark.parametrize("document", list(Document))
def test_the_bot_sends_a_document_whole_within_message_limits(document: Document) -> None:
    messages = [m.as_kwargs() for m in legal.document_messages(document, 18)]
    assert len(messages) > 1  # far longer than one message
    for message in messages:
        assert text_length(str(message["text"])) <= MESSAGE_LIMIT
        assert message["parse_mode"] is None  # entities only, never parsed
    sent = "\n\n".join(str(m["text"]) for m in messages)
    # Every block, in order.
    position = 0
    for block in document_blocks(document, 18):
        expected = f"• {block.text}" if block.kind == "item" else block.text
        position = sent.index(expected, position)
    assert effective_label() in str(messages[0]["text"])
    # Headings are bold.
    assert document_title(document) in _bold(messages[0])
    sections = {b.text for b in document_blocks(document, 18) if b.kind == "section"}
    assert sections <= set().union(*(_bold(m) for m in messages))


def test_the_welcome_names_the_documents_and_continue() -> None:
    assert legal.welcome_text(updated=False) == (
        "👋 Welcome to RealChat\n"
        "Real people. Real conversations.\n\n"
        "🎭 Anonymous 1-on-1 chats with real people. Your Telegram name, username, and phone "
        "number stay private.\n\n"
        "By tapping ✅ Continue you confirm your agreement with our Privacy Policy and User "
        "Agreement."
    )
    assert legal.welcome_text(updated=True).startswith(legal.UPDATED_TITLE)


def test_the_welcome_links_the_documents_once_the_address_is_set() -> None:
    base = HttpUrl("https://realchat.example")
    kwargs = legal.welcome(base, updated=False).as_kwargs()
    links = {e.url for e in kwargs["entities"] if e.type == "text_link"}
    assert links == {"https://realchat.example/privacy", "https://realchat.example/terms"}
    unlinked = legal.welcome(None, updated=False).as_kwargs()["entities"]
    assert all(e.type != "text_link" for e in unlinked)


def test_the_document_buttons_link_or_read_in_the_bot() -> None:
    [linked] = legal.documents_keyboard(HttpUrl("https://realchat.example/")).inline_keyboard
    assert [(b.text, b.url) for b in linked] == [
        ("🔒 Privacy Policy", "https://realchat.example/privacy"),
        ("📜 User Agreement", "https://realchat.example/terms"),
    ]
    [in_bot] = legal.documents_keyboard(None).inline_keyboard
    assert [b.callback_data for b in in_bot] == [
        legal.LegalAction(document="privacy_policy").pack(),
        legal.LegalAction(document="user_agreement").pack(),
    ]


def test_continue_is_a_blue_keyboard_button() -> None:
    [[button]] = legal.continue_keyboard().keyboard
    assert (button.text, button.style) == ("✅ Continue", "primary")
