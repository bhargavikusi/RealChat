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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.logging import exception_summary
from backend.modules.admin import directory
from backend.modules.admin.models import REASON_MAX_LENGTH
from backend.modules.moderation import actions, lookup, review
from backend.modules.moderation.actions import MODERATION_ROLES
from backend.modules.moderation.models import Report
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


class NotFound(Exception):
    """No such report, or the item doesn't belong to it."""


class Invalid(Exception):
    """The request doesn't fit the action (e.g. a rule missing, or unknown days)."""


class Conflict(Exception):
    """The action no longer applies (e.g. the report was closed meanwhile)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TelegramUnavailable(Exception):
    """Telegram refused or couldn't be reached."""


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
    telegram_user_id: int
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
    return StaffMember(user_id, telegram_user_id, role, nickname)


class ActionRequest(BaseModel):
    """An action on a report. Lifting is not offered here: it belongs to account
    lookup."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["close", "warn", "suspend", "ban"]
    rule: str | None = None
    days: int | None = None


Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=5, max_length=REASON_MAX_LENGTH)
]


class AccountActionRequest(BaseModel):
    """An action on a looked-up account, with no report: lifting is offered here."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["warn", "suspend", "ban", "lift"]
    rule: str | None = None
    days: int | None = None


class ReasonRequest(BaseModel):
    """Why an admin is opening an account's stored chats (saved in the audit log)."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason


class ThreadRequest(ReasonRequest):
    kind: Literal["chat", "direct"]
    ref: int


class ThreadMediaRequest(ReasonRequest):
    source: Literal[review.Source.CHAT, review.Source.DIRECT]
    item_id: int


class UsersQuery(BaseModel):
    """The user directory's search, filters, sorting, and page, as query parameters.
    Every value is checked here; anything else is refused."""

    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None
    vip: bool | None = None
    status: Literal["active", "suspended", "banned"] | None = None
    role: Literal["user", "moderator", "admin"] | None = None
    active_within_days: Annotated[int, Field(ge=1, le=3650)] | None = None
    min_chats: Annotated[int, Field(ge=0, le=100_000)] | None = None
    joined_from: date | None = None
    joined_to: date | None = None
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    gender: Literal["male", "female", "other"] | None = None
    min_age: Annotated[int, Field(ge=0, le=150)] | None = None
    max_age: Annotated[int, Field(ge=0, le=150)] | None = None
    min_coins: Annotated[int, Field(ge=-10_000_000, le=10_000_000)] | None = None
    max_coins: Annotated[int, Field(ge=-10_000_000, le=10_000_000)] | None = None
    min_reports: Annotated[int, Field(ge=0, le=100_000)] | None = None
    sort: directory.Sort = directory.Sort.NEWEST
    descending: bool = True
    page: Annotated[int, Field(ge=0, le=10_000)] = 0

    def filters(self) -> directory.Filters:
        values = self.model_dump()
        values["query"] = values["query"] or None
        return directory.Filters(**values)


class MediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: review.Source
    item_id: int | None = None


async def send_media(bot: Bot, chat_id: int, kind: str, file_id: str) -> int:
    """Send one file to a moderator's chat by its Telegram reference, protected from
    forwarding and saving; returns the new message's id."""
    senders: dict[str, Callable[..., Awaitable[Message]]] = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "animation": bot.send_animation,
        "audio": bot.send_audio,
        "voice": bot.send_voice,
        "video_note": bot.send_video_note,
        "sticker": bot.send_sticker,
        "document": bot.send_document,
    }
    sent = await senders[kind](chat_id, file_id, protect_content=True)
    return sent.message_id


async def _take_back(bot: Bot, chat_id: int, message_id: int) -> None:
    """Delete a copy sent to a moderator whose view could not be recorded."""
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as exc:
        logger.error("could not take back media sent to a moderator", extra=exception_summary(exc))


async def _show_media(
    session: AsyncSession,
    request: Request,
    member: StaffMember,
    found: review.Media,
    context: str,
    source: review.Source,
    item_id: int | None,
    reason: str | None = None,
) -> dict[str, int]:
    """Send a media item to the moderator's own chat, then audit the view and queue the
    copy's deletion; if that can't be recorded, take the copy back."""
    await session.commit()  # no transaction is held across the Telegram call
    bot: Bot = request.app.state.bot
    try:
        message_id = await send_media(bot, member.telegram_user_id, found.kind, found.file_id)
    except TelegramAPIError as exc:
        logger.warning("could not send media to a moderator", extra=exception_summary(exc))
        raise TelegramUnavailable from None
    try:
        await review.record_media_view(
            session,
            member.user_id,
            context,
            source,
            item_id,
            found,
            member.telegram_user_id,
            message_id,
            datetime.now(UTC),
            reason,
        )
        await session.commit()
    except Exception:
        # Without its audit entry and queued deletion the copy must not stay: take it
        # back now, best effort, and fail the request.
        await session.rollback()
        await _take_back(bot, member.telegram_user_id, message_id)
        raise
    return {"deleted_after_seconds": int(review.MEDIA_VIEW.total_seconds())}


def create_router(token: str) -> APIRouter:
    router = APIRouter(prefix=API_PATH)

    async def staff(request: Request) -> StaffMember:
        telegram_user_id = verify_launch_data(
            token, request.headers.get("Authorization"), datetime.now(UTC)
        )
        sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
        async with sessions() as session:
            return await load_staff(session, telegram_user_id)

    Staff = Annotated[StaffMember, Depends(staff)]

    async def database(request: Request) -> AsyncIterator[AsyncSession]:
        sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
        async with sessions() as session:
            yield session

    Database = Annotated[AsyncSession, Depends(database)]

    @router.get("/me")
    async def me(member: Staff) -> dict[str, str | None]:
        """Who is signed in, for the panel's header."""
        return {"role": member.role, "nickname": member.nickname}

    @router.get("/reports")
    async def reports(member: Staff, session: Database) -> list[review.QueueItem]:
        """Open reports, urgent first, then oldest first."""
        return await review.open_reports(session)

    @router.get("/reports/{report_id}")
    async def report(report_id: int, member: Staff, session: Database) -> review.Review:
        """One report with everything needed to decide on it. Audited."""
        found = await review.review(session, member.user_id, report_id, datetime.now(UTC).date())
        if found is None:
            raise NotFound
        await session.commit()
        return found

    @router.post("/reports/{report_id}/media")
    async def media(
        report_id: int, body: MediaRequest, member: Staff, session: Database, request: Request
    ) -> dict[str, int]:
        """Send one media item from the report to the moderator's own Telegram chat,
        deleted after `review.MEDIA_VIEW`. Audited."""
        found = await review.media_for(session, report_id, body.source, body.item_id)
        if found is None:
            raise NotFound
        return await _show_media(
            session, request, member, found, f"report {report_id}", body.source, body.item_id
        )

    @router.post("/reports/{report_id}/action")
    async def action(
        report_id: int, body: ActionRequest, member: Staff, session: Database
    ) -> dict[str, str]:
        """Take an action on the report's account (or close it with none), through the
        same rules as every moderation action (`actions.act`)."""
        target = await session.scalar(select(Report.reported_user_id).where(Report.id == report_id))
        if target is None:
            raise NotFound
        return await _act(
            session, member, target, body.kind, body.rule, body.days, report_id=report_id
        )

    async def _act(
        session: AsyncSession,
        member: StaffMember,
        target: int,
        kind: str,
        rule: str | None,
        days: int | None,
        report_id: int | None = None,
    ) -> dict[str, str]:
        try:
            outcome = await actions.act(
                session,
                member.user_id,
                target,
                actions.Kind(kind),
                datetime.now(UTC),
                report_id=report_id,
                rule=rule,
                days=days,
            )
        except ValueError:
            raise Invalid from None
        if outcome.refusal is not None:
            await session.rollback()
            raise Conflict(outcome.refusal.value)
        await session.commit()
        return {"result": "applied"}

    def admin(member: StaffMember) -> None:
        """Stored chats outside reports are for admins only (D-071)."""
        if member.role != "admin":
            raise Forbidden

    @router.get("/accounts")
    async def find_accounts(
        query: Annotated[str, Query(min_length=1, max_length=64)], member: Staff, session: Database
    ) -> list[lookup.Match]:
        """The account with this RealChat ID, as a list of zero or one. Audited."""
        found = await lookup.find(session, member.user_id, query)
        await session.commit()
        return found

    @router.get("/accounts/{account_id}")
    async def get_account(account_id: int, member: Staff, session: Database) -> lookup.Account:
        """An account's card, status, record, and reports."""
        found = await lookup.account(session, member.user_id, account_id, datetime.now(UTC).date())
        if found is None:
            raise NotFound
        await session.commit()
        return found

    @router.post("/accounts/{account_id}/action")
    async def account_action(
        account_id: int, body: AccountActionRequest, member: Staff, session: Database
    ) -> dict[str, str]:
        """Warn, suspend, ban, or lift, with no report, through `actions.act`."""
        if not await lookup.findable(session, account_id):
            raise NotFound
        return await _act(session, member, account_id, body.kind, body.rule, body.days)

    @router.post("/accounts/{account_id}/threads")
    async def account_threads(
        account_id: int, body: ReasonRequest, member: Staff, session: Database
    ) -> list[lookup.Thread]:
        """The account's stored conversations and direct-message threads, without
        content. Admins only; audited with the reason."""
        admin(member)
        if not await lookup.findable(session, account_id):
            raise NotFound
        found = await lookup.threads(session, member.user_id, account_id, body.reason)
        await session.commit()
        return found

    @router.post("/accounts/{account_id}/threads/open")
    async def open_account_thread(
        account_id: int, body: ThreadRequest, member: Staff, session: Database
    ) -> list[review.Item]:
        """One of the account's threads. Admins only; audited with the reason."""
        admin(member)
        if not await lookup.findable(session, account_id):
            raise NotFound
        items = await lookup.open_thread(
            session, member.user_id, account_id, body.kind, body.ref, body.reason
        )
        if items is None:
            raise NotFound
        await session.commit()
        return items

    @router.post("/accounts/{account_id}/media")
    async def account_media(
        account_id: int,
        body: ThreadMediaRequest,
        member: Staff,
        session: Database,
        request: Request,
    ) -> dict[str, int]:
        """Send a media item from one of the account's threads to the admin's own chat.
        Admins only; audited with the reason."""
        admin(member)
        if not await lookup.findable(session, account_id):
            raise NotFound
        found = await lookup.media_for(session, account_id, body.source, body.item_id)
        if found is None:
            raise NotFound
        return await _show_media(
            session,
            request,
            member,
            found,
            f"account {account_id}",
            body.source,
            body.item_id,
            body.reason,
        )

    @router.get("/users")
    async def users(
        params: Annotated[UsersQuery, Query()], member: Staff, session: Database
    ) -> directory.Page:
        """The user directory: admins only; audited with the filters used (D-072)."""
        admin(member)
        page = await directory.list_users(
            session, member.user_id, params.filters(), datetime.now(UTC)
        )
        await session.commit()
        return page

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

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    @app.exception_handler(Invalid)
    async def invalid(request: Request, exc: Invalid) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid"})

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": exc.reason})

    @app.exception_handler(TelegramUnavailable)
    async def telegram_unavailable(request: Request, exc: TelegramUnavailable) -> JSONResponse:
        return JSONResponse(status_code=502, content={"error": "telegram"})

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
