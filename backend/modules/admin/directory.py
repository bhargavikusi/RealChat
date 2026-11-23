"""The user directory in the admin panel (D-072): every account lookup can reach, with
search, filters, sorting, and paging.

Like the other services, these take an AsyncSession and leave committing to the
caller. Only accounts that aren't deleted and finished onboarding are listed (the same
rule as account lookup). Activity is the number of chats started in the last 30 days
(owner decision), never Telegram online status. Telegram ids, usernames, and chat
content are never returned. Every listing is audited with the filters used.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select, and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.modules.chat.models import Conversation, ConversationParticipant
from backend.modules.coins.models import CoinTransaction
from backend.modules.moderation.lookup import FINDABLE
from backend.modules.moderation.models import Report
from backend.modules.moderation.review import audit
from backend.modules.users import public_ids
from backend.modules.users.models import Profile, User

PAGE_SIZE = 50
ACTIVITY_DAYS = 30


class Sort(StrEnum):
    NEWEST = "newest"  # sign-up date, newest first
    LAST_ACTIVE = "last_active"
    CHATS = "chats"  # chats started in the last 30 days
    COINS = "coins"
    REPORTS = "reports"


@dataclass(frozen=True)
class Filters:
    """What to list. None means no filter on that field."""

    query: str | None = None  # RealChat ID (exact) or part of a nickname
    vip: bool | None = None
    status: str | None = None  # "active", "suspended", or "banned"
    role: str | None = None
    active_within_days: int | None = None
    min_chats: int | None = None
    joined_from: date | None = None
    joined_to: date | None = None
    country: str | None = None
    gender: str | None = None
    min_age: int | None = None
    max_age: int | None = None
    min_coins: int | None = None
    max_coins: int | None = None
    min_reports: int | None = None
    sort: Sort = Sort.NEWEST
    descending: bool = True
    page: int = 0

    def describe(self) -> str:
        """The filters used, for the audit log (at most 300 characters)."""
        parts = [
            f"{name}={value}"
            for name, value in self.__dict__.items()
            if value is not None and name not in ("page",)
        ]
        return ", ".join(parts)[:300]


@dataclass(frozen=True)
class Row:
    id: int
    public_id: str
    nickname: str | None
    age: int | None
    gender: str | None
    country_code: str | None
    status: str
    role: str
    vip_until: datetime | None
    coins: int
    last_active_at: datetime
    chats_30d: int
    joined_at: datetime
    reports_received: int


@dataclass(frozen=True)
class Page:
    rows: list[Row]
    total: int
    page: int
    page_size: int


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query(filters: Filters, now: datetime) -> tuple[Select, dict[str, ColumnElement]]:  # type: ignore[type-arg]
    """The listing's query, unordered and unpaged, and its computed columns."""
    since = now - timedelta(days=ACTIVITY_DAYS)
    chats = (
        select(func.count())
        .select_from(ConversationParticipant)
        .join(Conversation, Conversation.id == ConversationParticipant.conversation_id)
        .where(ConversationParticipant.user_id == User.id, Conversation.started_at >= since)
        .correlate(User)
        .scalar_subquery()
    )
    coins = (
        select(func.coalesce(func.sum(CoinTransaction.amount), 0))
        .where(CoinTransaction.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    reports = (
        select(func.count())
        .select_from(Report)
        .where(Report.reported_user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    today = now.date()
    age = Profile.age_at_entry + func.date_part("year", func.age(today, Profile.age_entered_on))
    columns = {"chats": chats, "coins": coins, "reports": reports, "age": age}

    conditions: list[ColumnElement[bool]] = list(FINDABLE)
    if filters.query:
        text = filters.query.strip()
        public_id = public_ids.normalize(text)
        nickname = Profile.nickname.ilike(f"%{_escape_like(text)}%", escape="\\")
        conditions.append(or_(Profile.public_id == public_id, nickname) if public_id else nickname)
    if filters.vip is not None:
        vip = and_(User.vip_until.is_not(None), User.vip_until > now)
        conditions.append(vip if filters.vip else ~vip)
    if filters.status is not None:
        conditions.append(User.status == filters.status)
    if filters.role is not None:
        conditions.append(User.role == filters.role)
    if filters.active_within_days is not None:
        conditions.append(User.last_active_at >= now - timedelta(days=filters.active_within_days))
    if filters.min_chats is not None:
        conditions.append(chats >= filters.min_chats)
    if filters.joined_from is not None:
        conditions.append(func.date(User.created_at) >= filters.joined_from)
    if filters.joined_to is not None:
        conditions.append(func.date(User.created_at) <= filters.joined_to)
    if filters.country is not None:
        conditions.append(Profile.country_code == filters.country)
    if filters.gender is not None:
        conditions.append(Profile.gender == filters.gender)
    if filters.min_age is not None:
        conditions.append(age >= filters.min_age)
    if filters.max_age is not None:
        conditions.append(age <= filters.max_age)
    if filters.min_coins is not None:
        conditions.append(coins >= filters.min_coins)
    if filters.max_coins is not None:
        conditions.append(coins <= filters.max_coins)
    if filters.min_reports is not None:
        conditions.append(reports >= filters.min_reports)
    if filters.joined_from and filters.joined_to and filters.joined_from > filters.joined_to:
        conditions.append(false())

    query = (
        select(
            User,
            Profile,
            chats.label("chats"),
            coins.label("coins"),
            reports.label("reports"),
            age.label("age"),
        )
        .join(Profile, Profile.user_id == User.id)
        .where(*conditions)
    )
    return query, columns


async def list_users(
    session: AsyncSession, actor_user_id: int, filters: Filters, now: datetime
) -> Page:
    """One page of users matching `filters`. The listing is audited."""
    query, columns = _query(filters, now)
    order_by = {
        Sort.NEWEST: User.created_at,
        Sort.LAST_ACTIVE: User.last_active_at,
        Sort.CHATS: columns["chats"],
        Sort.COINS: columns["coins"],
        Sort.REPORTS: columns["reports"],
    }[filters.sort]
    ordered = order_by.desc() if filters.descending else order_by.asc()
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.execute(
        query.order_by(ordered, User.id.desc()).limit(PAGE_SIZE).offset(filters.page * PAGE_SIZE)
    )
    session.add(
        audit(
            actor_user_id, "users_listed", None, f"page {filters.page}", filters.describe() or None
        )
    )
    return Page(
        rows=[
            Row(
                id=user.id,
                public_id=profile.public_id,
                nickname=profile.nickname,
                age=None if age is None else int(age),
                gender=profile.gender,
                country_code=profile.country_code,
                status=user.status,
                role=user.role,
                vip_until=user.vip_until if user.vip_until and user.vip_until > now else None,
                coins=int(coins),
                last_active_at=user.last_active_at,
                chats_30d=int(chats),
                joined_at=user.created_at,
                reports_received=int(reports),
            )
            for user, profile, chats, coins, reports, age in rows.all()
        ],
        total=int(total or 0),
        page=filters.page,
        page_size=PAGE_SIZE,
    )
