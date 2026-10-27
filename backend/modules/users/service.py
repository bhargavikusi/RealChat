"""User and profile operations shared by the bot and any future API.

Database operations take an AsyncSession and leave committing to the caller, so a
caller can group several operations into one transaction. Profile setters and
helpers work on an already loaded Profile and do no I/O.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.users import validation
from backend.modules.users.models import Profile, TermsAcceptance, User
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
