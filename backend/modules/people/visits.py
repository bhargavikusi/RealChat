"""Profile visits and Visited you (D-053).

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids are internal `users.id` values. Each pair of users has at most one
visit row, holding the latest visit, so Visited you lists each visitor once, latest
first, however often they visited. Visits older than the retention period (an admin
setting) are not listed and are deleted by the worker (`VisitRetention`). Only
visitors who can still use RealChat are listed. Blocks hide nobody; the blocker's
entry is only marked, and a blocked visitor's visits are still announced (D-056).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import Select, delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.chat.models import Conversation
from backend.modules.chat.models import ConversationParticipant as Participant
from backend.modules.people.models import ProfileVisit
from backend.modules.people.service import PAGE_SIZE, blocked_by
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from backend.modules.users.service import Notification

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Visitor:
    user_id: int
    nickname: str
    visited_at: datetime  # the latest visit
    blocked: bool  # whether the viewer has blocked this visitor


@dataclass(frozen=True)
class Visitors:
    entries: list[Visitor]
    has_next: bool


async def record_visit(
    session: AsyncSession, visitor_id: int, visited_id: int, now: datetime
) -> bool:
    """Record that `visitor_id` opened `visited_id`'s card at `now`; returns whether it
    was recorded (not for a self-visit, or when either account is no longer active).

    Both accounts are locked FOR KEY SHARE (in id order) and must still be active. Account
    deletion locks the account row FOR UPDATE, so a visit and a deletion never overlap:
    a visit waiting on a deletion sees the account deleted and is skipped, instead of
    leaving a row behind the deletion's cleanup.
    """
    if visitor_id == visited_id:
        return False
    active = await session.scalars(
        select(User.id)
        .where(User.id.in_((visitor_id, visited_id)), User.status == "active")
        .order_by(User.id)
        .with_for_update(key_share=True)
    )
    if len(active.all()) < 2:
        return False
    await session.execute(
        insert(ProfileVisit)
        .values(visitor_user_id=visitor_id, visited_user_id=visited_id, last_visited_at=now)
        .on_conflict_do_update(
            index_elements=[ProfileVisit.visitor_user_id, ProfileVisit.visited_user_id],
            set_={"last_visited_at": now},
            # A delayed write never moves a visit back in time.
            where=ProfileVisit.last_visited_at < now,
        )
    )
    return True


async def should_notify(session: AsyncSession, visitor_id: int, visited_id: int) -> bool:
    """Whether a visit is announced to the visited user: not while the two are chatting
    together, and not with visit alerts off. A visit from someone the visited user
    blocked is announced too (D-056)."""
    active = await chat.active_chat(session, visitor_id)
    if active is not None and active.partner_id == visited_id:
        return False
    return (await users.notification_settings(session, visited_id))[Notification.VISIT_ALERTS]


def _eligible_visits(viewer_id: int, since: datetime) -> Select[int, datetime]:
    return (
        select(ProfileVisit.visitor_user_id, ProfileVisit.last_visited_at)
        .join(User, User.id == ProfileVisit.visitor_user_id)
        .join(Profile, Profile.user_id == ProfileVisit.visitor_user_id)
        .where(
            ProfileVisit.visited_user_id == viewer_id,
            ProfileVisit.last_visited_at >= since,
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
        )
    )


async def visitors(
    session: AsyncSession, viewer_id: int, since: datetime, page: int = 0
) -> Visitors:
    """One page of the viewer's visitors, latest visit first."""
    query = (
        _eligible_visits(viewer_id, since)
        .add_columns(Profile.nickname, blocked_by(viewer_id, ProfileVisit.visitor_user_id))
        .order_by(ProfileVisit.last_visited_at.desc(), ProfileVisit.visitor_user_id)
        .offset(max(page, 0) * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    )
    found = [
        Visitor(int(visitor), str(nickname), visited_at, bool(blocked))
        for visitor, visited_at, nickname, blocked in await session.execute(query)
    ]
    return Visitors(found[:PAGE_SIZE], has_next=len(found) > PAGE_SIZE)


async def visitor_profile(
    session: AsyncSession, viewer_id: int, visitor_id: int, since: datetime
) -> Profile | None:
    """The profile of someone who visited the viewer within the retention period.

    None otherwise, so an old or forged button opens nobody. Read in the same statement
    as the check, with the visitor's account row share-locked until the transaction ends,
    as for /userid: an account deletion either finishes first, and nobody is found, or
    waits until the card has been read.
    """
    query = (
        _eligible_visits(viewer_id, since)
        .add_columns(Profile)
        .where(ProfileVisit.visitor_user_id == visitor_id)
        .with_for_update(read=True, of=User)
        .execution_options(populate_existing=True)
    )
    found = (await session.execute(query)).first()
    return None if found is None else found.Profile


async def latest_conversation(session: AsyncSession, user_id: int, other_id: int) -> int | None:
    """The latest conversation the two users shared, for Block and Report on a card.

    Conversations from before either user's latest account deletion don't count, as for
    Past chats: a returning account starts afresh.
    """
    other = aliased(Participant)
    me, them = aliased(User), aliased(User)
    return await session.scalar(
        select(Participant.conversation_id)
        .join(
            other,
            (other.conversation_id == Participant.conversation_id) & (other.user_id == other_id),
        )
        .join(Conversation, Conversation.id == Participant.conversation_id)
        .join(me, me.id == user_id)
        .join(them, them.id == other_id)
        .where(
            Participant.user_id == user_id,
            or_(me.deleted_at.is_(None), Conversation.started_at > me.deleted_at),
            or_(them.deleted_at.is_(None), Conversation.started_at > them.deleted_at),
        )
        .order_by(Participant.conversation_id.desc())
        .limit(1)
    )


def retention_start(now: datetime, days: int) -> datetime:
    """The earliest visit time still kept and listed."""
    return now - timedelta(days=days)


# --- Retention -------------------------------------------------------------------------

RUN_KEY = "retention:profile_visits:last_run"
RUN_INTERVAL = timedelta(days=1)
# How long a started run holds the key: the retry delay after a failure or crash.
RETRY_INTERVAL = timedelta(hours=1)
BATCH_SIZE = 1_000


async def delete_expired_visits(session: AsyncSession, cutoff: datetime, limit: int) -> int:
    """Delete up to `limit` visits older than `cutoff`; returns how many were deleted."""
    expired = (
        select(ProfileVisit.visitor_user_id, ProfileVisit.visited_user_id)
        .where(ProfileVisit.last_visited_at < cutoff)
        .limit(limit)
        # A visit being updated right now is left for the next run.
        .with_for_update(skip_locked=True)
        # Materialized, so the batch is chosen once. As a plain subquery the planner
        # may run it again for every row it scans, deleting more than `limit`.
        .cte("expired")
        .prefix_with("MATERIALIZED")
    )
    result = await session.execute(
        delete(ProfileVisit)
        .where(
            ProfileVisit.last_visited_at < cutoff,
            tuple_(ProfileVisit.visitor_user_id, ProfileVisit.visited_user_id).in_(
                select(expired.c.visitor_user_id, expired.c.visited_user_id)
            ),
        )
        .returning(ProfileVisit.visitor_user_id)
    )
    return len(result.all())


class VisitRetention:
    """Deletes expired visits once a day, like the chat retention job (D-046)."""

    def __init__(
        self,
        redis: Redis,
        sessions: async_sessionmaker[AsyncSession],
        runtime_settings: RuntimeSettings,
    ) -> None:
        self._redis = redis
        self._sessions = sessions
        self._runtime = runtime_settings

    async def tick(self, now: datetime) -> int | None:
        """Run the cleanup if it is due; returns the number deleted, or None if not due."""
        due = await self._redis.set(
            RUN_KEY, now.isoformat(), nx=True, ex=int(RETRY_INTERVAL.total_seconds())
        )
        if not due:
            return None
        deleted = await self.run(now)
        await self._redis.set(RUN_KEY, now.isoformat(), ex=int(RUN_INTERVAL.total_seconds()))
        return deleted

    async def run(self, now: datetime) -> int:
        """Delete every expired visit, batch by batch; returns how many were deleted."""
        async with self._sessions() as session:
            days = await self._runtime.get(session, SettingKey.VISITS_DAYS)
            await session.commit()
        cutoff = retention_start(now, days)
        total = 0
        while True:
            async with self._sessions() as session:
                deleted = await delete_expired_visits(session, cutoff, BATCH_SIZE)
                await session.commit()
            total += deleted
            if deleted < BATCH_SIZE:
                break
            await self._redis.set(RUN_KEY, now.isoformat(), ex=int(RETRY_INTERVAL.total_seconds()))
        logger.info("deleted expired profile visits", extra={"count": total})
        return total
