"""The moderation Mini App's API and sign-in, against a real database (D-071)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api import moderation
from apps.api.main import create_app
from backend.config.settings import BotSettings
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from tests.support.webapp import authorization

Sessions = async_sessionmaker[AsyncSession]
MODERATOR = 7101
ADMIN = 7102
PLAIN = 7103


@pytest.fixture
def client(
    bot_settings: BotSettings,
    session_factory: Sessions,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    # Not built: an empty app folder, whatever is built locally (see the tests below
    # for a built app).
    monkeypatch.setattr(moderation, "DIST_DIR", tmp_path)
    with TestClient(create_app(bot_settings)) as test_client:
        yield test_client


async def _account(sessions: Sessions, telegram_id: int, role: str, nickname: str) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        user.role = role
        await session.execute(
            update(Profile).where(Profile.user_id == user.id).values(nickname=nickname)
        )
        await session.commit()
        return user.id


def _signed(telegram_id: int, age: timedelta = timedelta(0)) -> dict[str, str]:
    return authorization(telegram_id, datetime.now(UTC) - age)


async def test_moderators_and_admins_are_signed_in(
    client: TestClient, session_factory: Sessions
) -> None:
    await _account(session_factory, MODERATOR, "moderator", "Night Owl")
    await _account(session_factory, ADMIN, "admin", "Moon Child")

    for telegram_id, expected in (
        (MODERATOR, {"role": "moderator", "nickname": "Night Owl"}),
        (ADMIN, {"role": "admin", "nickname": "Moon Child"}),
    ):
        response = client.get("/api/moderation/me", headers=_signed(telegram_id))
        assert (response.status_code, response.json()) == (200, expected)


async def test_users_without_a_role_or_account_are_refused(
    client: TestClient, session_factory: Sessions
) -> None:
    await _account(session_factory, PLAIN, "user", "Plain Jane")
    for telegram_id in (PLAIN, 99_999):  # 99_999 has never started the bot
        response = client.get("/api/moderation/me", headers=_signed(telegram_id))
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_a_restricted_moderator_is_refused(
    client: TestClient, session_factory: Sessions, status: str
) -> None:
    user_id = await _account(session_factory, MODERATOR, "moderator", "Night Owl")
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == user_id).values(status=status))
        await session.commit()

    assert client.get("/api/moderation/me", headers=_signed(MODERATOR)).status_code == 403


async def test_a_removed_role_is_refused_on_the_next_request(
    client: TestClient, session_factory: Sessions
) -> None:
    user_id = await _account(session_factory, MODERATOR, "moderator", "Night Owl")
    headers = _signed(MODERATOR)
    assert client.get("/api/moderation/me", headers=headers).status_code == 200

    async with session_factory() as session:
        await session.execute(update(User).where(User.id == user_id).values(role="user"))
        await session.commit()

    # The same launch data, still fresh, no longer works.
    assert client.get("/api/moderation/me", headers=headers).status_code == 403


async def test_missing_or_stale_launch_data_is_unauthorized(
    client: TestClient, session_factory: Sessions
) -> None:
    await _account(session_factory, MODERATOR, "moderator", "Night Owl")
    for headers in (
        {},
        {"Authorization": "tma forged"},
        _signed(MODERATOR, moderation.MAX_AGE + timedelta(minutes=1)),
    ):
        response = client.get("/api/moderation/me", headers=headers)
        assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})


async def test_api_responses_carry_the_panel_headers(
    client: TestClient, session_factory: Sessions
) -> None:
    await _account(session_factory, MODERATOR, "moderator", "Night Owl")
    for headers in (_signed(MODERATOR), {}):
        response = client.get("/api/moderation/me", headers=headers)
        for name, value in moderation.PANEL_HEADERS.items():
            assert response.headers[name] == value
    assert moderation.PANEL_HEADERS["Cache-Control"] == "no-store"
    assert (
        "frame-ancestors https://web.telegram.org"
        in (moderation.PANEL_HEADERS["Content-Security-Policy"])
    )
    # Other endpoints keep their own headers.
    assert "Content-Security-Policy" not in client.get("/health").headers


def test_the_panel_is_unavailable_until_built(client: TestClient) -> None:
    assert client.get("/moderation/").status_code == 404


def test_the_built_panel_is_served_with_its_headers(
    bot_settings: BotSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Moderation</title>")
    monkeypatch.setattr(moderation, "DIST_DIR", tmp_path)

    with TestClient(create_app(bot_settings)) as built:
        response = built.get("/moderation/")

    assert response.status_code == 200
    assert "<title>Moderation</title>" in response.text
    for name, value in moderation.PANEL_HEADERS.items():
        assert response.headers[name] == value
