"""RealChat's User Agreement and Privacy Policy (D-015, D-070).

Both are written in `legal/` as plain text with headings, from which the bot and the
web pages are rendered. Changing either requires a new TERMS_VERSION, so users accept
it again; users accept both together, by tapping Continue.

Only describe what the product actually does. Periods that admins can change, such as
message retention, are not given as numbers, so the text stays true when they change
(D-046).
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Literal

TERMS_VERSION = "1.2"
EFFECTIVE_DATE = date(2026, 11, 24)

LEGAL_DIR = Path(__file__).parent / "legal"


class Document(StrEnum):
    USER_AGREEMENT = "user_agreement"
    PRIVACY_POLICY = "privacy_policy"


BlockKind = Literal["title", "section", "subsection", "paragraph", "item"]


@dataclass(frozen=True)
class Block:
    """One heading, paragraph, or list item of a document."""

    kind: BlockKind
    text: str


@cache
def _source(document: Document) -> str:
    return (LEGAL_DIR / f"{document.value}.md").read_text(encoding="utf-8")


def document_blocks(document: Document, minimum_age: int) -> list[Block]:
    """The document as blocks: `# ` is its title, `## ` a section, `### ` a subsection,
    `- ` a list item, and any other line a paragraph. Blank lines only separate."""
    prefixes: tuple[tuple[str, BlockKind], ...] = (
        ("### ", "subsection"),
        ("## ", "section"),
        ("# ", "title"),
        ("- ", "item"),
    )
    blocks: list[Block] = []
    for line in _source(document).format(minimum_age=minimum_age).splitlines():
        line = line.strip()
        if not line:
            continue
        kind: BlockKind = "paragraph"
        for prefix, prefixed in prefixes:
            if line.startswith(prefix):
                kind, line = prefixed, line[len(prefix) :]
                break
        blocks.append(Block(kind, line))
    return blocks


def document_title(document: Document) -> str:
    return document_blocks(document, 0)[0].text


def effective_label() -> str:
    """The version line every rendering of the documents shows."""
    return f"Version {TERMS_VERSION} · Effective {EFFECTIVE_DATE.day} {EFFECTIVE_DATE:%B %Y}"


# The most Telegram accepts in one message, counted in UTF-16 code units.
MESSAGE_LIMIT = 4096


def text_length(text: str) -> int:
    """A text's length as Telegram counts it."""
    return len(text.encode("utf-16-le")) // 2
