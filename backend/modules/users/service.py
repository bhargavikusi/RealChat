"""User and profile operations shared by the bot and any future API.

Database operations take an AsyncSession and leave committing to the caller, so a
caller can group several operations into one transaction. Profile setters and
helpers work on an already loaded Profile and do no I/O.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.admin.models import AdminAuditEntry
from backend.modules.coins import rewards
from backend.modules.coins import service as coins
from backend.modules.direct_messages.models import DirectMessage
from backend.modules.people.models import ProfileLike, ProfileVisit
from backend.modules.users import public_ids, validation
from backend.modules.users.models import (
    RESTRICTED_STATUSES,
    NotificationSettings,
    Profile,
    ProfileChange,
    TermsAcceptance,
    User,
)
from backend.modules.users.validation import Visibility


async def get_or_create_user(session: AsyncSession, telegram_user_id: int) -> User:
    """Return the account for a Telegram user, creating it (and an empty profile) once.

    Safe under concurrency: simultaneous calls for the same Telegram user create a
    single row, because the insert yields to the unique constraint instead of failing.
    """
    await session.execute(
        insert(User)
        .values(telegram_user_id=telegram_user_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
    )
    user = (
        await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    ).scalar_one()
    await _ensure_profile(session, user.id)
    return user


async def _ensure_profile(session: AsyncSession, user_id: int) -> None:
    """Give the user an empty profile with a new RealChat ID, unless they have one.

    The insert yields to any conflict: a profile created at the same moment, or an ID
    already taken, in which case another ID is drawn.
    """
    while await session.scalar(select(Profile.user_id).where(Profile.user_id == user_id)) is None:
        await session.execute(
            insert(Profile)
            .values(user_id=user_id, public_id=public_ids.new_public_id())
            .on_conflict_do_nothing()
        )


async def find_profile(session: AsyncSession, public_id: str) -> Profile | None:
    """The profile with this RealChat ID, if its account is active and onboarded.

    Unknown IDs and unavailable accounts give the same answer, so a lookup reveals
    nothing about accounts that cannot be opened (D-054). The account row is share-locked
    until the transaction ends, so an account deletion (which locks it for update)
    either finishes first, and the profile is not found, or waits until the card is read.
    """
    return await session.scalar(
        select(Profile)
        .join(User, User.id == Profile.user_id)
        .where(
            Profile.public_id == public_id,
            User.status == "active",
            Profile.onboarding_completed_at.is_not(None),
        )
        .with_for_update(read=True, of=User)
        .execution_options(populate_existing=True)
    )


async def user_id_for(session: AsyncSession, public_id: str) -> int | None:
    """The account that currently has this RealChat ID, whatever its status."""
    return await session.scalar(select(Profile.user_id).where(Profile.public_id == public_id))


async def record_activity(
    session: AsyncSession, telegram_user_id: int, now: datetime, every: timedelta
) -> None:
    """Note that an active account used RealChat at `now` (D-051).

    Written only when the stored time is at least `every` old, so a busy user causes at
    most one write per interval. This is RealChat activity, never Telegram presence.
    """
    await session.execute(
        update(User)
        .where(
            User.telegram_user_id == telegram_user_id,
            User.status == "active",
            User.last_active_at <= now - every,
        )
        .values(last_active_at=now)
    )


async def get_user(session: AsyncSession, user_id: int) -> User:
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


async def lock_user(session: AsyncSession, user_id: int) -> User:
    """Load the user with their row locked for update until the transaction ends.

    Matching share-locks the same row (`chat.service.eligible_for_matching`), so a
    deletion that locks first and a match never interleave.
    """
    # populate_existing: the caller may have loaded this user earlier in the session,
    # and the status and role must be read under the lock, not from that copy.
    return (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def get_profile(session: AsyncSession, user_id: int) -> Profile:
    return (await session.execute(select(Profile).where(Profile.user_id == user_id))).scalar_one()


async def has_accepted_terms(session: AsyncSession, user_id: int, terms_version: str) -> bool:
    found = await session.execute(
        select(TermsAcceptance.id).where(
            TermsAcceptance.user_id == user_id, TermsAcceptance.terms_version == terms_version
        )
    )
    return found.first() is not None


async def accept_terms(session: AsyncSession, user_id: int, terms_version: str) -> None:
    """Record acceptance of a terms version. Repeating it keeps the first timestamp.

    A deleted account that accepts the terms again becomes active: this is how a
    returning user re-registers (D-025). Restricted accounts are never reactivated.
    """
    await session.execute(
        insert(TermsAcceptance)
        .values(user_id=user_id, terms_version=terms_version)
        .on_conflict_do_nothing(index_elements=["user_id", "terms_version"])
    )
    await session.execute(
        update(User).where(User.id == user_id, User.status == "deleted").values(status="active")
    )


async def get_status(session: AsyncSession, telegram_user_id: int) -> str | None:
    """The account status for a Telegram user, or None if they have no account."""
    return await session.scalar(
        select(User.status).where(User.telegram_user_id == telegram_user_id)
    )


def is_restricted(status: str | None) -> bool:
    return status in RESTRICTED_STATUSES


async def delete_account(session: AsyncSession, user_id: int, now: datetime) -> None:
    """Erase a user's personal data and keep a minimal account record (D-016, D-025).

    Erased: every profile field (the profile is replaced by an empty one with a new
    RealChat ID, so the old ID finds nobody, D-054), the age
    and gender change history, notification settings, profile visits and likes made or
    received (D-053, D-056), direct messages sent or received (D-057), and terms acceptances, so a
    returning user starts a fresh onboarding with the default settings. Coins are
    forfeited (held coins released, then the balance removed by one ledger entry), the
    invite code is retired, and pending invites are settled as not rewarded (D-055).
    Kept: the Telegram user ID, the status, timestamps, and the coin ledger. An
    active account becomes "deleted"; a suspended or banned one keeps its status, so
    deleting cannot be used to escape a restriction. A moderator or admin role is reset
    to "user" and the change is audit-logged, so a returning account has no special
    access until it is granted again (D-034).
    """
    user = await lock_user(session, user_id)
    await session.execute(delete(ProfileChange).where(ProfileChange.user_id == user_id))
    await session.execute(delete(TermsAcceptance).where(TermsAcceptance.user_id == user_id))
    await session.execute(delete(Profile).where(Profile.user_id == user_id))
    await session.execute(
        delete(NotificationSettings).where(NotificationSettings.user_id == user_id)
    )
    await session.execute(
        delete(ProfileVisit).where(
            or_(ProfileVisit.visitor_user_id == user_id, ProfileVisit.visited_user_id == user_id)
        )
    )
    await session.execute(
        delete(ProfileLike).where(
            or_(ProfileLike.liker_user_id == user_id, ProfileLike.liked_user_id == user_id)
        )
    )
    # A message still pending holds its sender's coin: release it before the row goes,
    # whichever of the two is deleting. The rows are locked first, so a delivery being
    # settled right now either finishes before this or finds the message gone.
    involved = or_(
        DirectMessage.sender_user_id == user_id, DirectMessage.recipient_user_id == user_id
    )
    pending_holds = await session.scalars(
        select(DirectMessage.hold_id)
        .where(involved, DirectMessage.status == "pending", DirectMessage.hold_id.is_not(None))
        .with_for_update()
    )
    for hold_id in pending_holds.all():
        if hold_id is not None:
            await coins.release(session, hold_id, now)
    await session.execute(delete(DirectMessage).where(involved))
    await _ensure_profile(session, user_id)
    user.coin_history_after_id = (await coins.forfeit_all(session, user_id, now)).history_after_id
    await rewards.retire_invites(session, user_id, now)
    if user.status not in RESTRICTED_STATUSES:
        user.status = "deleted"
    user.deleted_at = now
    if user.role != "user":
        session.add(
            AdminAuditEntry(
                actor_user_id=user_id,
                action="role_changed",
                target_user_id=user_id,
                old_value=user.role,
                new_value="user",
            )
        )
        user.role = "user"


class Notification(StrEnum):
    """The notification settings a user can change (D-052), by column name."""

    SILENT_MODE = "silent_mode"
    VISIT_ALERTS = "visit_alerts"
    LIKE_ALERTS = "like_alerts"
    REMINDERS = "reminders"


NOTIFICATION_DEFAULTS = {
    Notification.SILENT_MODE: False,
    Notification.VISIT_ALERTS: True,
    Notification.LIKE_ALERTS: True,
    Notification.REMINDERS: True,
}


@dataclass(frozen=True)
class NotificationChoices:
    values: dict[Notification, bool]

    def __getitem__(self, setting: Notification) -> bool:
        return self.values[setting]


async def notification_settings(session: AsyncSession, user_id: int) -> NotificationChoices:
    """The user's notification settings, with the defaults for anything never changed."""
    row = await session.get(NotificationSettings, user_id, populate_existing=True)
    if row is None:
        return NotificationChoices(dict(NOTIFICATION_DEFAULTS))
    return NotificationChoices({setting: bool(getattr(row, setting)) for setting in Notification})


async def set_notification(
    session: AsyncSession, user_id: int, setting: Notification, value: bool
) -> None:
    """Set one notification setting. Setting the value it already has changes nothing,
    so a repeated or old button cannot flip it back."""
    await session.execute(
        insert(NotificationSettings)
        .values(user_id=user_id, **{setting.value: value})
        .on_conflict_do_update(
            index_elements=[NotificationSettings.user_id],
            set_={setting.value: value, "updated_at": func.now()},
        )
    )


class ChangeCooldownError(Exception):
    """An age or gender change was requested before the cooldown ended (D-019)."""

    def __init__(self, field: str, available_at: datetime) -> None:
        super().__init__(f"{field} can be changed again at {available_at.isoformat()}")
        self.field = field
        self.available_at = available_at


# Field setters validate first, so invalid input never reaches the database.


def set_nickname(profile: Profile, raw: str) -> None:
    profile.nickname = validation.validate_nickname(raw)


def set_age(profile: Profile, age: int, minimum_age: int, today: date) -> None:
    profile.age_at_entry = validation.validate_age(age, minimum_age)
    profile.age_entered_on = today


def set_gender(profile: Profile, raw: str) -> None:
    profile.gender = validation.validate_gender(raw).value


def set_country(profile: Profile, raw_code: str) -> None:
    profile.country_code = validation.validate_country_code(raw_code)


def set_city(profile: Profile, raw: str | None) -> None:
    profile.city = None if raw is None else validation.validate_city(raw)


def set_bio(profile: Profile, raw: str | None) -> None:
    profile.bio = None if raw is None else validation.validate_bio(raw)


def set_photo(profile: Profile, file_id: str | None) -> None:
    profile.photo_file_id = file_id


def set_visibility(profile: Profile, visibility: Visibility) -> None:
    profile.visibility = visibility.value


def profile_age(profile: Profile, today: date) -> int | None:
    if profile.age_at_entry is None or profile.age_entered_on is None:
        return None
    return validation.current_age(profile.age_at_entry, profile.age_entered_on, today)


def missing_required_fields(profile: Profile) -> list[str]:
    """Required onboarding fields (D-013) not yet provided, in onboarding order."""
    required = {
        "nickname": profile.nickname,
        "age": profile.age_at_entry,
        "gender": profile.gender,
        "country": profile.country_code,
    }
    return [name for name, value in required.items() if value is None]


# --- Cooldown-limited changes after onboarding (D-019, D-024) ------------------------


async def change_available_at(
    session: AsyncSession, user_id: int, field: str, cooldown: timedelta
) -> datetime | None:
    """When `field` may next be changed, or None if it has never been changed.

    The first change after onboarding is always allowed, so typos can be fixed.
    """
    last = await session.scalar(
        select(func.max(ProfileChange.changed_at)).where(
            ProfileChange.user_id == user_id, ProfileChange.field == field
        )
    )
    return None if last is None else last + cooldown


async def change_age(
    session: AsyncSession,
    user_id: int,
    age: int,
    *,
    minimum_age: int,
    now: datetime,
    cooldown: timedelta,
) -> bool:
    """Change the age of an onboarded profile. Returns False if it is unchanged.

    Raises ProfileValidationError or ChangeCooldownError; nothing is written then.
    """
    validation.validate_age(age, minimum_age)
    profile = await _lock_profile(session, user_id)
    old = profile_age(profile, now.date())
    if old is None:
        raise ValueError("age can only be changed after it has been set")
    if old == age:
        return False
    await _check_cooldown(session, user_id, "age", now, cooldown)
    set_age(profile, age, minimum_age, now.date())
    _record_change(session, user_id, "age", str(old), str(age), now)
    return True


async def change_gender(
    session: AsyncSession, user_id: int, raw: str, *, now: datetime, cooldown: timedelta
) -> bool:
    """Change the gender of an onboarded profile. Returns False if it is unchanged.

    Raises ProfileValidationError or ChangeCooldownError; nothing is written then.
    """
    gender = validation.validate_gender(raw).value
    profile = await _lock_profile(session, user_id)
    old = profile.gender
    if old is None:
        raise ValueError("gender can only be changed after it has been set")
    if old == gender:
        return False
    await _check_cooldown(session, user_id, "gender", now, cooldown)
    profile.gender = gender
    _record_change(session, user_id, "gender", old, gender, now)
    return True


async def _lock_profile(session: AsyncSession, user_id: int) -> Profile:
    # Row lock held until commit: concurrent changes for the same user run one after
    # the other, so the second sees the first's history row and hits the cooldown.
    return (
        await session.execute(
            select(Profile)
            .where(Profile.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _check_cooldown(
    session: AsyncSession, user_id: int, field: str, now: datetime, cooldown: timedelta
) -> None:
    available_at = await change_available_at(session, user_id, field, cooldown)
    if available_at is not None and now < available_at:
        raise ChangeCooldownError(field, available_at)


def _record_change(
    session: AsyncSession, user_id: int, field: str, old: str, new: str, now: datetime
) -> None:
    session.add(
        ProfileChange(user_id=user_id, field=field, old_value=old, new_value=new, changed_at=now)
    )
