"""User and profile operations shared by the bot and any future API.

Database operations take an AsyncSession and leave committing to the caller, so a
caller can group several operations into one transaction. Profile setters and
helpers work on an already loaded Profile and do no I/O.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.users import validation
from backend.modules.users.models import Profile, ProfileChange, TermsAcceptance, User
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
    await session.execute(
        insert(Profile)
        .values(user_id=user.id)
        .on_conflict_do_nothing(index_elements=[Profile.user_id])
    )
    return user


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
    """Record acceptance of a terms version. Repeating it keeps the first timestamp."""
    await session.execute(
        insert(TermsAcceptance)
        .values(user_id=user_id, terms_version=terms_version)
        .on_conflict_do_nothing(index_elements=["user_id", "terms_version"])
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
