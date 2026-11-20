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
from backend.modules.users.terms import (
    PRIVACY_HEADING,
    TERMS_HEADING,
    TERMS_VERSION,
    privacy_notice,
    terms_of_use,
)


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
    ("path", "heading", "other"),
    [("/terms", TERMS_HEADING, "/privacy"), ("/privacy", PRIVACY_HEADING, "/terms")],
)
def test_the_terms_and_privacy_pages(
    client: TestClient, path: str, heading: str, other: str
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
    assert f"<h1>{heading}</h1>" in page
    assert "DRAFT - not for launch" in page
    assert f"Version {TERMS_VERSION}" in page
    assert f'<a href="{other}">' in page
    assert "<script" not in page


def test_the_pages_show_the_current_text(client: TestClient) -> None:
    with client:
        terms = client.get("/terms").text
        privacy = client.get("/privacy").text
    # The configured age is filled in; apostrophes and the like are escaped.
    assert "<li>You must be at least 21 years old.</li>" in terms
    # Every bullet, word for word, and nothing more.
    for page, text in ((terms, terms_of_use(21)), (privacy, privacy_notice())):
        bullets = [line[2:] for line in text.split("\n") if line.startswith("• ")]
        assert page.count("<li>") == len(bullets)
        for bullet in bullets:
            assert f"<li>{escape(bullet)}</li>" in page
    assert "you&#x27;ve chatted with" in privacy


def test_page_text_is_escaped() -> None:
    page = pages.render("A <b>", "Intro & more\n• one <script>\n• two", ("/x", "X"))
    assert "<p>Intro &amp; more</p>" in page
    assert "<ul><li>one &lt;script&gt;</li><li>two</li></ul>" in page
    assert "<title>RealChat · A &lt;b&gt;</title>" in page
