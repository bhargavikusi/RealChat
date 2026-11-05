"""The welcome bonus and invite rewards (D-050).

* Welcome bonus: granted when a user completes their profile, once per Telegram account
  ever. The account row is kept after deletion, so a returning user never gets it again.
* Invites: each user has a personal invite code. Starting the bot through someone's
  invite link records them as the inviter, if the new user has never completed a
  profile before (and never had an account deleted). When the invited user completes
  their profile, the inviter is rewarded, unless they have reached the daily cap or are
  restricted. The invited user gets only the welcome bonus, never a second reward.

Like the other services, these take an AsyncSession and leave committing to the
caller. Every reward is a ledger entry with an idempotency key, so a repeated or
concurrent completion rewards once.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.coins import service as coins
from backend.modules.coins.models import InviteCode, Referral
from backend.modules.users.models import Profile, User

INVITE_PREFIX = "inv_"


async def invite_code(session: AsyncSession, user_id: int) -> str:
    """The user's invite code, created the first time it is asked for."""
    existing = await session.scalar(select(InviteCode.code).where(InviteCode.user_id == user_id))
    if existing is not None:
        return existing
    while True:
        # 8 URL-safe characters; a collision just tries again.
        code = secrets.token_urlsafe(6)
        await session.execute(
            insert(InviteCode).values(user_id=user_id, code=code).on_conflict_do_nothing()
        )
        stored = await session.scalar(select(InviteCode.code).where(InviteCode.user_id == user_id))
        if stored is not None:
            return stored


async def attach_invite(session: AsyncSession, invitee_id: int, payload: str) -> int | None:
    """Record who invited a user from their /start payload; returns the inviter's id if
    this call recorded it.

    Nothing is recorded for an unknown code, a self-invite, a user who already has an
    inviter, or a user who has completed a profile before or deleted an account.
    """
    if not payload.startswith(INVITE_PREFIX):
        return None
    inviter_id = await session.scalar(
        select(InviteCode.user_id).where(InviteCode.code == payload.removeprefix(INVITE_PREFIX))
    )
    if inviter_id is None or inviter_id == invitee_id:
        return None
    is_new = await session.scalar(
        select(User.id)
        .join(Profile, Profile.user_id == User.id)
        .where(
            User.id == invitee_id,
            User.deleted_at.is_(None),
            Profile.onboarding_completed_at.is_(None),
        )
    )
    if is_new is None:
        return None
    recorded = await session.scalar(
        insert(Referral)
        .values(invitee_user_id=invitee_id, inviter_user_id=inviter_id)
        .on_conflict_do_nothing(index_elements=["invitee_user_id"])
        .returning(Referral.inviter_user_id)
    )
    return None if recorded is None else int(recorded)


@dataclass(frozen=True)
class Granted:
    welcome: int  # coins the new user got (0 if none)
    inviter_id: int | None = None  # set if an inviter was rewarded
    invite_reward: int = 0


async def on_profile_complete(
    session: AsyncSession,
    user_id: int,
    now: datetime,
    *,
    welcome: int,
    invite_reward: int,
    invites_per_day: int,
) -> Granted:
    """Grant the welcome bonus and settle any invite, once (D-050)."""
    welcomed = welcome > 0 and await coins.credit(
        session, user_id, welcome, "welcome_bonus", f"welcome:{user_id}"
    )
    referral = (
        await session.execute(
            select(Referral.inviter_user_id)
            .where(Referral.invitee_user_id == user_id, Referral.status == "pending")
            .with_for_update()
        )
    ).scalar_one_or_none()
    if referral is None:
        return Granted(welcome if welcomed else 0)
    inviter_id = int(referral)
    # The inviter's row is locked while counting, so two invitees completing at once
    # cannot both pass the cap.
    inviter_status = (
        await session.execute(
            select(User.status).where(User.id == inviter_id).with_for_update(key_share=True)
        )
    ).scalar_one()
    start_of_day = datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)
    rewarded_today = await session.scalar(
        select(func.count())
        .select_from(Referral)
        .where(
            Referral.inviter_user_id == inviter_id,
            Referral.status == "rewarded",
            Referral.settled_at >= start_of_day,
        )
    )
    rewarded = (
        invite_reward > 0
        and inviter_status == "active"
        and (rewarded_today or 0) < invites_per_day
        and await coins.credit(
            session, inviter_id, invite_reward, "referral_reward", f"referral:{user_id}"
        )
    )
    await session.execute(
        update(Referral)
        .where(Referral.invitee_user_id == user_id, Referral.status == "pending")
        .values(status="rewarded" if rewarded else "not_rewarded", settled_at=now)
    )
    if not rewarded:
        return Granted(welcome if welcomed else 0)
    return Granted(welcome if welcomed else 0, inviter_id, invite_reward)
