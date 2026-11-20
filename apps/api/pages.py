"""The User Agreement and the Privacy Policy as web pages (D-069, D-070).

Both come from the same source as the bot's (`backend.modules.users.terms`), so the
pages never drift from what users accept. Each page is self-contained: no scripts,
fonts, images, or tracking, and a content security policy that allows nothing else.
"""

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from backend.modules.users.terms import Block, Document, document_blocks, effective_label

STYLE = """
body { font: 16px/1.6 system-ui, sans-serif; max-width: 46rem; margin: 2rem auto;
  padding: 0 1rem; color: #1a1a1a; background: #fff; }
h1 { font-size: 1.9rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.3rem; margin-top: 2rem; }
h3 { font-size: 1.05rem; margin-top: 1.25rem; }
.meta { opacity: 0.75; margin-top: 0; }
ul { padding-left: 1.25rem; } li { margin-bottom: 0.4rem; }
footer { margin-top: 2.5rem; font-size: 0.875rem; opacity: 0.8; }
@media (prefers-color-scheme: dark) { body { color: #e6e6e6; background: #121212; }
  a { color: #8ab4f8; } }
"""
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}
PATHS = {Document.USER_AGREEMENT: "/terms", Document.PRIVACY_POLICY: "/privacy"}
TAGS = {"title": "h1", "section": "h2", "subsection": "h3", "paragraph": "p"}


def _body(blocks: list[Block]) -> str:
    """Headings and paragraphs in their tags, consecutive list items as one list, all
    escaped. The version line follows the title."""
    parts: list[str] = []
    items: list[str] = []
    for block in blocks:
        if block.kind == "item":
            items.append(f"<li>{escape(block.text)}</li>")
            continue
        if items:
            parts.append(f"<ul>{''.join(items)}</ul>")
            items = []
        tag = TAGS[block.kind]
        parts.append(f"<{tag}>{escape(block.text)}</{tag}>")
        if block.kind == "title":
            parts.append(f'<p class="meta">{escape(effective_label())}</p>')
    if items:
        parts.append(f"<ul>{''.join(items)}</ul>")
    return "\n".join(parts)


def render(document: Document, minimum_age: int) -> str:
    """A document's page, with a link to the other document."""
    blocks = document_blocks(document, minimum_age)
    other = next(d for d in Document if d is not document)
    other_title = document_blocks(other, minimum_age)[0].text
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>RealChat · {escape(blocks[0].text)}</title>\n<style>{STYLE}</style>\n"
        f"</head>\n<body>\n{_body(blocks)}\n"
        f'<footer>RealChat · <a href="{PATHS[other]}">{escape(other_title)}</a></footer>\n'
        "</body>\n</html>\n"
    )


def create_router(minimum_age: int) -> APIRouter:
    router = APIRouter()

    @router.get(PATHS[Document.USER_AGREEMENT], response_class=HTMLResponse)
    async def user_agreement() -> HTMLResponse:
        return HTMLResponse(render(Document.USER_AGREEMENT, minimum_age), headers=HEADERS)

    @router.get(PATHS[Document.PRIVACY_POLICY], response_class=HTMLResponse)
    async def privacy_policy() -> HTMLResponse:
        return HTMLResponse(render(Document.PRIVACY_POLICY, minimum_age), headers=HEADERS)

    return router
