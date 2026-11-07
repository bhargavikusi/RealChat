"""Profile likes, Liked you, and You liked (D-056).

Like the other services, these take an AsyncSession and leave committing to the
caller. User ids are internal `users.id` values. Each pair of users has at most one
like row: liking sets `liked_at`, unliking clears it, so a like is never counted
twice. The row is kept after an unlike so that `notified_at` still limits how often
the liked user is told: at most one notice per pair within the gap (an admin setting).
Likes last until unliked or either account is deleted. Only accounts that can still use
RealChat are listed. Blocks hide nobody; the blocker's entry is only marked. A user
cannot like someone they blocked.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from backend.modules.moderation import service as moderation
from backend.modules.people.models import ProfileLike
from backend.modules.people.service import PAGE_SIZE, blocked_by
from backend.modules.users.models import Profile, User


class LikeOutcome(StrEnum):
    LIKED = "liked"  # a new like
    ALREADY = "already"  # already liked: nothing changed
    BLOCKED = "blocked"  # the liker blocked this person
    UNAVAILABLE = "unavailable"  # either account can't use RealChat, or a self-like


@dataclass(frozen=True)
class Liked:
    outcome: LikeOutcome
    # Whether this like may be announced: the pair's notice gap has passed. Claimed
    # here, so concurrent likes never send two notices.
    notify: bool = False


@dataclass(frozen=True)
class LikeEntry:
    user_id: int
    nickname: str
    liked_at: datetime
    blocked: bool  # whether the viewer has blocked this person


@dataclass(frozen=True)
class LikeEntries:
    entries: list[LikeEntry]
    has_next: bool


async def like(
    session: AsyncSession,
    liker_id: int,
    liked_id: int,
    now: datetime,
    notice_gap: timedelta,
    *,
    announce: bool = True,
) -> Liked:
    """`liker_id` likes `liked_id`'s profile.

    Both accounts are locked FOR KEY SHARE (in id order) and must be active with a
    finished profile, so a like and an account deletion never overlap, as for visits.
    A repeated or concurrent like changes nothing. With `announce` (the liked user wants
    like alerts), a new like claims the pair's notice unless one was sent within
    `notice_gap`.
    """
    if liker_id == liked_id:
        return Liked(LikeOutcome.UNAVAILABLE)
    ready = await session.scalars(
        select(User.id)
        .join(Profile, Profile.user_id == User.id)
        .where(
            User.id.in_((liker_id, liked_id)),
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
        )
        .order_by(User.id)
        .with_for_update(key_share=True, of=User)
    )
    if len(ready.all()) < 2:
        return Liked(LikeOutcome.UNAVAILABLE)
    if await moderation.is_blocked(session, liker_id, liked_id):
        return Liked(LikeOutcome.BLOCKED)
    added = await session.scalar(
        insert(ProfileLike)
        .values(liker_user_id=liker_id, liked_user_id=liked_id, liked_at=now)
        .on_conflict_do_update(
            index_elements=[ProfileLike.liker_user_id, ProfileLike.liked_user_id],
            set_={"liked_at": now},
            where=ProfileLike.liked_at.is_(None),
        )
        .returning(ProfileLike.liker_user_id)
    )
    if added is None:
        return Liked(LikeOutcome.ALREADY)
    if not announce:
        return Liked(LikeOutcome.LIKED)
    claimed = await session.scalar(
        update(ProfileLike)
        .where(
            ProfileLike.liker_user_id == liker_id,
            ProfileLike.liked_user_id == liked_id,
            or_(ProfileLike.notified_at.is_(None), ProfileLike.notified_at <= now - notice_gap),
        )
        .values(notified_at=now)
        .returning(ProfileLike.liker_user_id)
    )
    return Liked(LikeOutcome.LIKED, notify=claimed is not None)


async def unlike(session: AsyncSession, liker_id: int, liked_id: int) -> bool:
    """Remove `liker_id`'s like of `liked_id`; returns whether there was one. Silent."""
    removed = await session.scalar(
        update(ProfileLike)
        .where(
            ProfileLike.liker_user_id == liker_id,
            ProfileLike.liked_user_id == liked_id,
            ProfileLike.liked_at.is_not(None),
        )
        .values(liked_at=None)
        .returning(ProfileLike.liker_user_id)
    )
    return removed is not None


async def is_liked(session: AsyncSession, liker_id: int, liked_id: int) -> bool:
    found = await session.scalar(
        select(ProfileLike.liker_user_id).where(
            ProfileLike.liker_user_id == liker_id,
            ProfileLike.liked_user_id == liked_id,
            ProfileLike.liked_at.is_not(None),
        )
    )
    return found is not None


def _eligible(
    mine: InstrumentedAttribute[int], theirs: InstrumentedAttribute[int], viewer_id: int
) -> Select[int, datetime | None]:
    """(other user, liked_at) for the viewer's current likes in one direction: `mine` is
    the viewer's side of the pair, `theirs` the other user's, who must still be able to
    use RealChat."""
    return (
        select(theirs, ProfileLike.liked_at)
        .join(User, User.id == theirs)
        .join(Profile, Profile.user_id == theirs)
        .where(
            mine == viewer_id,
            ProfileLike.liked_at.is_not(None),
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
        )
    )


def _likers(viewer_id: int) -> Select[int, datetime | None]:
    return _eligible(ProfileLike.liked_user_id, ProfileLike.liker_user_id, viewer_id)


def _liked(viewer_id: int) -> Select[int, datetime | None]:
    return _eligible(ProfileLike.liker_user_id, ProfileLike.liked_user_id, viewer_id)


async def _page(
    session: AsyncSession,
    query: Select[int, datetime | None],
    other: InstrumentedAttribute[int],
    viewer_id: int,
    page: int,
) -> LikeEntries:
    rows = (
        query.add_columns(Profile.nickname, blocked_by(viewer_id, other))
        .order_by(ProfileLike.liked_at.desc(), other)
        .offset(max(page, 0) * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    )
    found = [
        LikeEntry(int(user), str(nickname), liked_at, bool(blocked))
        for user, liked_at, nickname, blocked in await session.execute(rows)
    ]
    return LikeEntries(found[:PAGE_SIZE], has_next=len(found) > PAGE_SIZE)


async def likers(session: AsyncSession, viewer_id: int, page: int = 0) -> LikeEntries:
    """One page of Liked you: everyone who likes the viewer, latest like first."""
    return await _page(session, _likers(viewer_id), ProfileLike.liker_user_id, viewer_id, page)


async def liked(session: AsyncSession, viewer_id: int, page: int = 0) -> LikeEntries:
    """One page of You liked: everyone the viewer likes, latest like first."""
    return await _page(session, _liked(viewer_id), ProfileLike.liked_user_id, viewer_id, page)


async def _profile(
    session: AsyncSession,
    query: Select[int, datetime | None],
    other: InstrumentedAttribute[int],
    other_id: int,
) -> Profile | None:
    found = (
        await session.execute(
            query.add_columns(Profile)
            .where(other == other_id)
            .execution_options(populate_existing=True)
        )
    ).first()
    return None if found is None else found.Profile


async def liker_profile(session: AsyncSession, viewer_id: int, liker_id: int) -> Profile | None:
    """The profile of someone who currently likes the viewer; None otherwise, so an old
    or forged button opens nobody. Read in the same statement as the check, as for
    visits."""
    return await _profile(session, _likers(viewer_id), ProfileLike.liker_user_id, liker_id)


async def liked_profile(session: AsyncSession, viewer_id: int, liked_id: int) -> Profile | None:
    """The profile of someone the viewer currently likes; None otherwise."""
    return await _profile(session, _liked(viewer_id), ProfileLike.liked_user_id, liked_id)
