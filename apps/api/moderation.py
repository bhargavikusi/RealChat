"""The moderation Mini App: Telegram sign-in, its API, and the built app (D-071).

Every API request carries the Mini App's launch data from Telegram in the header
`Authorization: tma <launch data>`. The server checks Telegram's signature with the
bot token (the documented check, as implemented by aiogram), refuses data older than
`MAX_AGE`, and re-reads the account's role and status on every request. There are no
sessions or cookies, so there is nothing to steal or forge and no cross-site request
forgery; a moderator whose role is removed, or who is suspended, is refused on their
next request.

The built app (`apps/webapp/dist`) is served at `/moderation/` when it exists.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.moderation.actions import MODERATION_ROLES
from backend.modules.users.models import Profile, User

logger = logging.getLogger(__name__)

# Launch data is accepted for this long after the panel was opened (owner decision);
# after that the moderator reopens it from the bot.
MAX_AGE = timedelta(hours=1)
# Launch data dated this far in the future is still accepted, for clock differences.
CLOCK_SKEW = timedelta(minutes=1)
SCHEME = "tma "

APP_PATH = "/moderation"
API_PATH = "/api/moderation"
DIST_DIR = Path(__file__).resolve().parents[1] / "webapp" / "dist"

# The panel's own files and Telegram's official Mini App script, nothing else. Telegram
# Web shows Mini Apps in a frame, so only it may frame the panel.
PANEL_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; "
    "style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class Unauthorized(Exception):
    """The launch data is missing, forged, or too old: reopen the panel from the bot."""


class Forbidden(Exception):
    """The account may not use the panel."""


def verify_launch_data(token: str, header: str | None, now: datetime) -> int:
    """The Telegram user id in a valid `Authorization: tma ...` header.

    Raises Unauthorized if the header is missing or malformed, the signature doesn't
    match this bot's token, there is no user, or the data is older than MAX_AGE (or
    dated in the future beyond CLOCK_SKEW).
    """
    if header is None or not header.startswith(SCHEME):
        raise Unauthorized
    try:
        data = safe_parse_webapp_init_data(token, header.removeprefix(SCHEME))
    except (ValueError, ValidationError):
        raise Unauthorized from None
    if data.user is None:
        raise Unauthorized
    signed_at = data.auth_date.astimezone(UTC)
    if signed_at < now - MAX_AGE or signed_at > now + CLOCK_SKEW:
        raise Unauthorized
    return data.user.id


@dataclass(frozen=True)
class StaffMember:
    user_id: int
    role: str
    nickname: str | None


async def load_staff(session: AsyncSession, telegram_user_id: int) -> StaffMember:
    """The signed-in account, if it may use the panel: an active moderator or admin
    (not suspended, banned, or deleted). Raises Forbidden otherwise."""
    row = (
        await session.execute(
            select(User.id, User.role, User.status, Profile.nickname)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.telegram_user_id == telegram_user_id)
        )
    ).one_or_none()
    if row is None:
        raise Forbidden
    user_id, role, status, nickname = row
    if role not in MODERATION_ROLES or status != "active":
        raise Forbidden
    return StaffMember(user_id, role, nickname)


def create_router(token: str) -> APIRouter:
    router = APIRouter(prefix=API_PATH)

    async def staff(request: Request) -> StaffMember:
        telegram_user_id = verify_launch_data(
            token, request.headers.get("Authorization"), datetime.now(UTC)
        )
        sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
        async with sessions() as session:
            return await load_staff(session, telegram_user_id)

    @router.get("/me")
    async def me(member: Annotated[StaffMember, Depends(staff)]) -> dict[str, str | None]:
        """Who is signed in, for the panel's header."""
        return {"role": member.role, "nickname": member.nickname}

    return router


def install(app: FastAPI, token: str, dist_dir: Path | None = None) -> None:
    """Add the moderation API, the built panel when it exists (`DIST_DIR` unless given),
    and their headers."""
    dist_dir = DIST_DIR if dist_dir is None else dist_dir

    @app.exception_handler(Unauthorized)
    async def unauthorized(request: Request, exc: Unauthorized) -> JSONResponse:
        # Only that it failed: never which check, so it can't be probed.
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error": "forbidden"})

    @app.middleware("http")
    async def panel_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if path == APP_PATH or path.startswith((f"{APP_PATH}/", f"{API_PATH}/")):
            response.headers.update(PANEL_HEADERS)
        return response

    app.include_router(create_router(token))
    if (dist_dir / "index.html").is_file():
        app.mount(APP_PATH, StaticFiles(directory=dist_dir, html=True), name="moderation")
    else:
        logger.info("the moderation app is not built; /moderation/ is unavailable")
