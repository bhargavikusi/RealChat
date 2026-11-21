"""Moderation actions: closing reports, warnings, timed suspensions, bans, and lifting
them (D-071).

Like the other services, these take an AsyncSession and leave committing to the
caller, and user ids are internal `users.id` values. An action is checked and applied
under row locks, so a repeated or concurrent request applies once:

* the actor's and the target's account rows are locked first, in id order, so the
  actor's role is read as it stands and two actions on one account never interleave;
* then the report, when given, which must still be open, so it is closed by exactly
  one action; and then the target's chat, if the action ends it. Users before
  conversations is the order account deletion and reports use, so none of them
  deadlock with an action;
* everything the action owes someone (its notice, the reporter's, and the chat partner's
  when a chat ended) is written as a `ModerationNotice` in the same transaction and
  sent by the worker after the commit.

Suspensions end by themselves: `lift_if_expired` lifts one when the user's next update
arrives, and `lift_expired` lifts the rest in the worker.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.admin.models import AdminAuditEntry
from backend.modules.chat import service as chat
from backend.modules.moderation.models import (
    RULES,
    SUSPENSION_DAYS,
    ModerationAction,
    ModerationNotice,
    Report,
)
from backend.modules.users import service as users
from backend.modules.users.models import RESTRICTED_STATUSES, User

MODERATION_ROLES = ("moderator", "admin")
# Actions that restrict the account, and so end its chat.
RESTRICTING = ("suspend", "ban")
# Actions on the account, as opposed to only closing the report.
ON_ACCOUNT = ("warn", "suspend", "ban")
AUDIT_ACTIONS = {
    "close": "report_closed",
    "warn": "account_warned",
    "suspend": "account_suspended",
    "ban": "account_banned",
    "lift": "restriction_lifted",
}


class Kind(StrEnum):
    CLOSE = "close"
    WARN = "warn"
    SUSPEND = "suspend"
    BAN = "ban"
    LIFT = "lift"


class Refusal(StrEnum):
    """Why an action was not applied. Nothing changed in any of these cases."""

    NOT_ALLOWED = "not_allowed"  # the actor isn't a moderator or admin, or is restricted
    ADMIN_ONLY = "admin_only"  # only admins lift bans
    PROTECTED = "protected"  # the target is the actor, or a moderator acting on staff
    REPORT_CLOSED = "report_closed"  # the report was closed already (or doesn't match)
    BANNED = "banned"  # a banned account can't be suspended or banned again
    NOT_RESTRICTED = "not_restricted"  # nothing to lift


@dataclass(frozen=True)
class Outcome:
    action_id: int | None = None
    refusal: Refusal | None = None

    @property
    def applied(self) -> bool:
        return self.refusal is None


async def _lock_pair(session: AsyncSession, first: int, second: int) -> dict[int, User]:
    """Both account rows, locked in id order."""
    locked = {}
    for user_id in sorted({first, second}):
        locked[user_id] = await users.lock_user(session, user_id)
    return locked


async def _lock_open_report(
    session: AsyncSession, report_id: int, target_user_id: int
) -> Report | None:
    """The report, locked, if it is still open and is about the target."""
    report = (
        await session.execute(
            select(Report)
            .where(Report.id == report_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if report is None or report.status != "open" or report.reported_user_id != target_user_id:
        return None
    return report


def _refusal(actor: User, target: User, kind: Kind) -> Refusal | None:
    """Why the actor may not take this action on the target as things stand, if so."""
    if actor.role not in MODERATION_ROLES or actor.status in RESTRICTED_STATUSES:
        return Refusal.NOT_ALLOWED
    if actor.id == target.id or (actor.role != "admin" and target.role in MODERATION_ROLES):
        return Refusal.PROTECTED
    if kind is Kind.LIFT:
        if target.status not in RESTRICTED_STATUSES:
            return Refusal.NOT_RESTRICTED
        if target.status == "banned" and actor.role != "admin":
            return Refusal.ADMIN_ONLY
    if kind in (Kind.SUSPEND, Kind.BAN) and target.status == "banned":
        return Refusal.BANNED
    return None


async def _status_after_lift(session: AsyncSession, user: User) -> str:
    """What a lifted account goes back to: "deleted" if it was deleted while restricted
    and hasn't come back since (deletion erases its terms acceptances), else "active"."""
    if user.deleted_at is not None and not await users.has_accepted_any_terms(session, user.id):
        return "deleted"
    return "active"


async def act(
    session: AsyncSession,
    actor_user_id: int,
    target_user_id: int,
    kind: Kind,
    now: datetime,
    *,
    report_id: int | None = None,
    rule: str | None = None,
    days: int | None = None,
) -> Outcome:
    """Take a moderation action, closing `report_id` with it when given.

    Raises ValueError for arguments that don't fit the kind: a rule is needed for a
    warning, suspension, or ban (and only then), days for a suspension, and a report
    for closing one. Returns a refusal, changing nothing, if the action isn't allowed
    or no longer applies.
    """
    if (kind.value in ON_ACCOUNT) != (rule is not None) or (rule is not None and rule not in RULES):
        raise ValueError("a rule is required for warnings, suspensions, and bans only")
    if (kind is Kind.SUSPEND) != (days is not None) or (
        days is not None and days not in SUSPENSION_DAYS
    ):
        raise ValueError(f"suspensions last one of {SUSPENSION_DAYS} days")
    if kind is Kind.CLOSE and report_id is None:
        raise ValueError("closing needs a report")

    locked = await _lock_pair(session, actor_user_id, target_user_id)
    actor, target = locked[actor_user_id], locked[target_user_id]
    refusal = _refusal(actor, target, kind)
    if refusal is not None:
        return Outcome(refusal=refusal)
    report = None
    if report_id is not None:
        report = await _lock_open_report(session, report_id, target_user_id)
        if report is None:
            return Outcome(refusal=Refusal.REPORT_CLOSED)

    old_status = target.status
    ends_at = None if days is None else now + timedelta(days=days)
    if kind is Kind.SUSPEND:
        target.status, target.suspended_until = "suspended", ends_at
    elif kind is Kind.BAN:
        target.status, target.suspended_until = "banned", None
    elif kind is Kind.LIFT:
        target.status, target.suspended_until = await _status_after_lift(session, target), None

    action = ModerationAction(
        actor_user_id=actor.id,
        target_user_id=target.id,
        report_id=report_id,
        kind=kind.value,
        rule=rule,
        days=days,
        ends_at=ends_at,
        created_at=now,
    )
    session.add(action)
    await session.flush()

    ended = None
    if kind.value in RESTRICTING:
        ended = await chat.end_conversation(session, target.id, now, "restricted")
    if report is not None:
        report.status, report.closed_at = "closed", now
        report.outcome = "action_taken" if kind.value in ON_ACCOUNT else "no_action"
        report.closed_by_action_id = action.id

    notices: list[tuple[str, int, int | None]] = []
    if kind is not Kind.CLOSE:
        notices.append(("action", target.id, None))
    if report is not None:
        notices.append(("reporter", report.reporter_user_id, None))
    if ended is not None:
        notices.append(("chat_ended", ended.partner_id, ended.conversation_id))
    for notice_kind, recipient, conversation_id in notices:
        session.add(
            ModerationNotice(
                action_id=action.id,
                kind=notice_kind,
                recipient_user_id=recipient,
                conversation_id=conversation_id,
                next_attempt_at=now,
            )
        )
    session.add(
        AdminAuditEntry(
            actor_user_id=actor.id,
            action=AUDIT_ACTIONS[kind.value],
            target_user_id=target.id,
            old_value=old_status,
            new_value=_audit_value(kind, rule, days, target.status, report_id),
        )
    )
    return Outcome(action_id=action.id)


def _audit_value(
    kind: Kind, rule: str | None, days: int | None, status: str, report_id: int | None
) -> str:
    """What the audit entry records as the new value (at most 64 characters)."""
    if kind is Kind.SUSPEND:
        return f"{rule} {days}d"
    if kind in (Kind.WARN, Kind.BAN):
        return str(rule)
    if kind is Kind.CLOSE:
        return f"report {report_id}"
    return status


async def lift_if_expired(session: AsyncSession, user_id: int, now: datetime) -> bool:
    """Lift the user's suspension if its end has passed; returns whether it did.

    The account row is locked first, so a lift and an account deletion (which locks the
    same row) happen one after the other, and the status after the lift is decided from
    the row as it stands. However many updates arrive together, it lifts once.
    """
    user = await users.lock_user(session, user_id)
    if user.status != "suspended" or user.suspended_until is None or user.suspended_until > now:
        return False
    user.status, user.suspended_until = await _status_after_lift(session, user), None
    return True


async def lift_expired(session: AsyncSession, now: datetime, limit: int) -> int:
    """Lift up to `limit` suspensions whose end has passed, the earliest first; returns
    how many. The accounts are locked in id order, as `act` locks them, so a batch and
    a moderator's action never deadlock."""
    due = await session.scalars(
        select(User.id)
        .where(User.status == "suspended", User.suspended_until <= now)
        .order_by(User.suspended_until)
        .limit(limit)
    )
    lifted = 0
    for user_id in sorted(due.all()):
        lifted += await lift_if_expired(session, user_id, now)
    return lifted


# A claimed notice that wasn't marked sent is tried again after this long, up to
# NOTICE_ATTEMPTS times in all; a notice Telegram can never deliver (the person blocked
# the bot) is marked sent at once.
NOTICE_RETRY = timedelta(minutes=5)
NOTICE_ATTEMPTS = 5


@dataclass(frozen=True)
class DueNotice:
    id: int
    kind: str  # "action", "reporter", or "chat_ended"
    telegram_user_id: int
    recipient_user_id: int
    action_kind: str
    target_user_id: int
    rule: str | None
    ends_at: datetime | None
    conversation_id: int | None


async def claim_notices(session: AsyncSession, now: datetime, limit: int) -> list[DueNotice]:
    """Claim up to `limit` notices that are due, oldest first. Each claim counts an
    attempt and moves the next one NOTICE_RETRY ahead, so a worker that stops before
    sending leaves the notice for a later tick, and two workers never claim it together
    (SKIP LOCKED). The caller commits the claim before sending."""
    due = (
        select(ModerationNotice.id)
        .where(
            ModerationNotice.sent_at.is_(None),
            ModerationNotice.next_attempt_at <= now,
            ModerationNotice.attempts < NOTICE_ATTEMPTS,
        )
        .order_by(ModerationNotice.next_attempt_at, ModerationNotice.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = (
        await session.scalars(
            update(ModerationNotice)
            .where(ModerationNotice.id.in_(due.scalar_subquery()))
            .values(next_attempt_at=now + NOTICE_RETRY, attempts=ModerationNotice.attempts + 1)
            .returning(ModerationNotice.id)
        )
    ).all()
    if not claimed:
        return []
    rows = await session.execute(
        select(
            ModerationNotice.id,
            ModerationNotice.kind,
            User.telegram_user_id,
            ModerationNotice.recipient_user_id,
            ModerationAction.kind,
            ModerationAction.target_user_id,
            ModerationAction.rule,
            ModerationAction.ends_at,
            ModerationNotice.conversation_id,
        )
        .join(ModerationAction, ModerationAction.id == ModerationNotice.action_id)
        .join(User, User.id == ModerationNotice.recipient_user_id)
        .where(ModerationNotice.id.in_(claimed))
        .order_by(ModerationNotice.id)
    )
    return [DueNotice(*row) for row in rows.all()]


async def mark_sent(session: AsyncSession, notice_id: int, now: datetime) -> None:
    await session.execute(
        update(ModerationNotice)
        .where(ModerationNotice.id == notice_id, ModerationNotice.sent_at.is_(None))
        .values(sent_at=now)
    )


@dataclass(frozen=True)
class Standing:
    status: str
    suspended_until: datetime | None


async def standing(session: AsyncSession, telegram_user_id: int, now: datetime) -> Standing | None:
    """The account's status for a Telegram user, lifting a suspension that has ended
    first, or None if they have no account. Used by the bot's access gate."""
    row = (
        await session.execute(
            select(User.id, User.status, User.suspended_until).where(
                User.telegram_user_id == telegram_user_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    user_id, status, until = row
    if status == "suspended" and until is not None and until <= now:
        await lift_if_expired(session, user_id, now)
        # Read back: another action may have changed the account meanwhile.
        status, until = (
            await session.execute(
                select(User.status, User.suspended_until).where(User.id == user_id)
            )
        ).one()
    return Standing(str(status), until)
