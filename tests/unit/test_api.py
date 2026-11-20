"""API checks that need no running services.

Creating the engine and Redis client does not open connections, so these tests
run without PostgreSQL or Redis as long as no endpoint touches them.
"""

from html import escape

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api import pages
from apps.api.main import create_app
from backend.config.settings import Settings
from backend.modules.users.terms import Block, Document, document_blocks, effective_label


@pytest.fixture
def client() -> TestClient:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=SecretStr("postgresql+asyncpg://user:unused@127.0.0.1:1/unused"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        min_age=21,
    )
    return TestClient(create_app(settings))


def test_health_does_not_depend_on_services(client: TestClient) -> None:
    with client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_docs_are_not_exposed(client: TestClient) -> None:
    with client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    ("path", "document", "other"),
    [
        ("/terms", Document.USER_AGREEMENT, "/privacy"),
        ("/privacy", Document.PRIVACY_POLICY, "/terms"),
    ],
)
def test_the_user_agreement_and_privacy_pages(
    client: TestClient, path: str, document: Document, other: str
) -> None:
    with client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for header, value in pages.HEADERS.items():
        assert response.headers[header] == value
    assert pages.HEADERS["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )
    assert pages.HEADERS["Referrer-Policy"] == "no-referrer"
    assert pages.HEADERS["Cache-Control"] == "no-cache"
    assert pages.HEADERS["X-Content-Type-Options"] == "nosniff"
    page = response.text
    assert f'<a href="{other}">' in page
    assert f'<p class="meta">{escape(effective_label())}</p>' in page
    assert "<script" not in page
    assert "DRAFT" not in page
    # Every block, word for word, in its own tag, with the configured age.
    tags = {"title": "h1", "section": "h2", "subsection": "h3", "paragraph": "p", "item": "li"}
    blocks = document_blocks(document, 21)
    for block in blocks:
        tag = tags[block.kind]
        assert f"<{tag}>{escape(block.text)}</{tag}>" in page
    assert page.count("<li>") == sum(1 for b in blocks if b.kind == "item")
    assert page.count("<h2>") == sum(1 for b in blocks if b.kind == "section")
    assert "21 years old" in page


def test_list_items_are_grouped_and_text_is_escaped() -> None:
    blocks = [
        Block("title", "A <b>"),
        Block("paragraph", "Intro & more"),
        Block("item", "one <script>"),
        Block("item", "two"),
        Block("section", "1. Next"),
    ]
    body = pages._body(blocks)
    assert "<h1>A &lt;b&gt;</h1>" in body
    assert "<p>Intro &amp; more</p>" in body
    assert "<ul><li>one &lt;script&gt;</li><li>two</li></ul>\n<h2>1. Next</h2>" in body


def test_the_page_title_and_footer_are_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    titles = {Document.USER_AGREEMENT: "Terms <b>", Document.PRIVACY_POLICY: "Privacy & <i>"}

    def blocks(document: Document, minimum_age: int) -> list[Block]:
        return [Block("title", titles[document]), Block("paragraph", "Text")]

    monkeypatch.setattr(pages, "document_blocks", blocks)
    page = pages.render(Document.USER_AGREEMENT, 18)
    assert "<title>RealChat · Terms &lt;b&gt;</title>" in page
    assert '<footer>RealChat · <a href="/privacy">Privacy &amp; &lt;i&gt;</a></footer>' in page
    assert "<b>" not in page and "<i>" not in page
