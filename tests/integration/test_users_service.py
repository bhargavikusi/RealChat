import asyncio
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.engine import Base
from backend.modules.users import service
from backend.modules.users.models import Profile, TermsAcceptance, User
from backend.modules.users.validation import ProfileValidationError, Visibility

TELEGRAM_ID = 555_000_111


async def test_get_or_create_user_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        first = await service.get_or_create_user(session, TELEGRAM_ID)
        second = await service.get_or_create_user(session, TELEGRAM_ID)
        await session.commit()

        assert first.id == second.id
        assert first.status == "active"
        profile = await service.get_profile(session, first.id)
        assert profile.visibility == "visible"
        assert service.missing_required_fields(profile) == ["nickname", "age", "gender", "country"]


async def test_concurrent_get_or_create_creates_one_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def create() -> int:
        async with session_factory() as session:
            user = await service.get_or_create_user(session, TELEGRAM_ID)
            await session.commit()
            return user.id

    ids = await asyncio.gather(*(create() for _ in range(10)))

    assert len(set(ids)) == 1
    async with session_factory() as session:
        users = await session.scalar(select(func.count()).select_from(User))
        profiles = await session.scalar(select(func.count()).select_from(Profile))
    assert (users, profiles) == (1, 1)


async def test_profile_fields_round_trip(session_factory: async_sessionmaker[AsyncSession]) -> None:
    entered = date(2026, 10, 27)
    async with session_factory() as session:
        user = await service.get_or_create_user(session, TELEGRAM_ID)
        profile = await service.get_profile(session, user.id)
        service.set_nickname(profile, "  Night   Owl ")
        service.set_age(profile, 24, minimum_age=18, today=entered)
        service.set_gender(profile, "female")
        service.set_country(profile, "in")
        service.set_city(profile, "Hyderabad")
        service.set_bio(profile, "Music and long walks.")
        service.set_photo(profile, "AgACAgIAAxkBAAIB-test-file-id")
        service.set_visibility(profile, Visibility.HIDDEN)
        profile.onboarding_completed_at = datetime.now(UTC)
        await session.commit()

    async with session_factory() as session:
        stored = await service.get_profile(session, user.id)
        assert (stored.nickname, stored.gender, stored.country_code) == (
            "Night Owl",
            "female",
            "IN",
        )
        assert (stored.age_at_entry, stored.age_entered_on) == (24, entered)
        assert service.profile_age(stored, date(2027, 10, 27)) == 25
        assert stored.visibility == "hidden"
        assert service.missing_required_fields(stored) == []


async def test_service_rejects_invalid_values_before_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user = await service.get_or_create_user(session, TELEGRAM_ID)
        profile = await service.get_profile(session, user.id)
        with pytest.raises(ProfileValidationError):
            service.set_age(profile, 16, minimum_age=18, today=date(2026, 10, 27))
        with pytest.raises(ProfileValidationError):
            service.set_country(profile, "ZZ")
        with pytest.raises(ProfileValidationError):
            service.set_gender(profile, "robot")
        assert profile.age_at_entry is None
        assert profile.country_code is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("nickname", "x"),
        ("nickname", "x" * 21),
        ("nickname", "  "),
        ("nickname", " Al"),
        ("city", "Paris "),
        ("age_at_entry", 12),
        ("age_at_entry", 100),
        ("gender", "robot"),
        ("country_code", "in"),
        ("visibility", "public"),
        ("bio", ""),
    ],
)
async def test_database_constraints_reject_invalid_values(
    session_factory: async_sessionmaker[AsyncSession], column: str, value: object
) -> None:
    # Bypasses the service on purpose: the database is the last line of defence.
    # The asyncpg dialect reports some data errors as a plain DBAPIError, so the
    # PostgreSQL error class is checked instead: 23 (integrity constraint violation)
    # or 22 (data exception, e.g. value too long). A connection failure has neither.
    async with session_factory() as session:
        user = await service.get_or_create_user(session, TELEGRAM_ID)
        await session.commit()
        extra = ", age_entered_on = current_date" if column == "age_at_entry" else ""
        # `column` comes from the fixed parametrize list above, never from input.
        with pytest.raises(DBAPIError) as excinfo:
            await session.execute(
                text(f"UPDATE profiles SET {column} = :value{extra} WHERE user_id = :id"),  # noqa: S608
                {"value": value, "id": user.id},
            )
        assert str(excinfo.value.orig.sqlstate)[:2] in {"22", "23"}  # type: ignore[union-attr]


async def test_completed_onboarding_requires_all_required_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user = await service.get_or_create_user(session, TELEGRAM_ID)
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE profiles SET onboarding_completed_at = now() WHERE user_id = :id"),
                {"id": user.id},
            )


async def test_terms_acceptance_is_recorded_once_per_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user = await service.get_or_create_user(session, TELEGRAM_ID)
        assert not await service.has_accepted_terms(session, user.id, "draft-1")

        await service.accept_terms(session, user.id, "draft-1")
        await service.accept_terms(session, user.id, "draft-1")
        await session.commit()

        assert await service.has_accepted_terms(session, user.id, "draft-1")
        assert not await service.has_accepted_terms(session, user.id, "draft-2")
        count = await session.scalar(select(func.count()).select_from(TermsAcceptance))
        assert count == 1


async def test_deleted_status_requires_deleted_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user_id = (await service.get_or_create_user(session, TELEGRAM_ID)).id
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE users SET status = 'deleted' WHERE id = :id"), {"id": user_id}
            )
        await session.rollback()
        await session.execute(
            text("UPDATE users SET status = 'deleted', deleted_at = now() WHERE id = :id"),
            {"id": user_id},
        )
        await session.commit()


TABLES = (
    "users",
    "profiles",
    "terms_acceptances",
    "profile_changes",
    "app_settings",
    "admin_audit_log",
    "conversations",
    "conversation_participants",
)


async def test_model_check_constraints_match_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # alembic check does not compare check constraints, so a constraint added to a
    # model but not to a migration (or vice versa) would otherwise go unnoticed.
    expected = {
        str(constraint.name)
        for name in TABLES
        for table in [Base.metadata.tables[name]]
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    async with session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE contype = 'c' "
                "AND conrelid::regclass::text = ANY(:tables)"
            ),
            {"tables": list(TABLES)},
        )
        actual = {row[0] for row in rows}
    assert actual == expected
