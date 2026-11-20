"""The terms of use and the privacy notice as web pages (D-069).

Both come from the same source as the bot's /terms (`backend.modules.users.terms`), so
the pages never drift from what users accept. Each page is self-contained: no scripts,
fonts, images, or tracking, and a content security policy that allows nothing else.
"""

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from backend.modules.users.terms import (
    DRAFT_LABEL,
    PRIVACY_HEADING,
    TERMS_HEADING,
    TERMS_VERSION,
    privacy_notice,
    terms_of_use,
)

STYLE = """
body { font: 16px/1.5 system-ui, sans-serif; max-width: 42rem; margin: 2rem auto;
  padding: 0 1rem; color: #1a1a1a; background: #fff; }
.draft { font-weight: bold; color: #b00020; }
@media (prefers-color-scheme: dark) { body { color: #e6e6e6; background: #121212; }
  a { color: #8ab4f8; } .draft { color: #ff8a80; } }
ul { padding-left: 1.25rem; } li { margin-bottom: 0.75rem; }
footer { margin-top: 2rem; font-size: 0.875rem; opacity: 0.8; }
"""
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def _body(text: str) -> str:
    """Bullets as a list, other lines as paragraphs, all escaped."""
    parts: list[str] = []
    bullets: list[str] = []
    for line in text.split("\n"):
        if line.startswith("• "):
            bullets.append(f"<li>{escape(line[2:])}</li>")
            continue
        if bullets:
            parts.append(f"<ul>{''.join(bullets)}</ul>")
            bullets = []
        if line:
            parts.append(f"<p>{escape(line)}</p>")
    if bullets:
        parts.append(f"<ul>{''.join(bullets)}</ul>")
    return "\n".join(parts)


def render(heading: str, text: str, other: tuple[str, str]) -> str:
    """A page with its heading, the draft label, the text, and a link to the other page."""
    other_path, other_title = other
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>RealChat · {escape(heading)}</title>\n<style>{STYLE}</style>\n</head>\n"
        f'<body>\n<p class="draft">{escape(DRAFT_LABEL)}</p>\n<h1>{escape(heading)}</h1>\n'
        f"{_body(text)}\n"
        f'<footer>Version {escape(TERMS_VERSION)} · <a href="{other_path}">'
        f"{escape(other_title)}</a></footer>\n</body>\n</html>\n"
    )


def create_router(minimum_age: int) -> APIRouter:
    router = APIRouter()

    @router.get("/terms", response_class=HTMLResponse)
    async def terms() -> HTMLResponse:
        page = render(TERMS_HEADING, terms_of_use(minimum_age), ("/privacy", PRIVACY_HEADING))
        return HTMLResponse(page, headers=HEADERS)

    @router.get("/privacy", response_class=HTMLResponse)
    async def privacy() -> HTMLResponse:
        page = render(PRIVACY_HEADING, privacy_notice(), ("/terms", TERMS_HEADING))
        return HTMLResponse(page, headers=HEADERS)

    return router
