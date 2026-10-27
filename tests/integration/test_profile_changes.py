"""Age and gender changes after onboarding: cooldown and change history (D-019, D-024)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.modules.users import service
from backend.modules.users.models import ProfileChange
from backend.modules.users.validation import ProfileValidationError

Sessions = async_sessionmaker[AsyncSession]
TELEGRAM_ID = 777_000_222
COOLDOWN = timedelta(days=30)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def user_id(session_factory: Sessions) -> int:
    """An onboarded user: 24-year-old female in India, age entered at T0."""
    async with session_factory() as session:
        user = await service.get_or_create_user(session, TELEGRAM_ID)
        profile = await service.get_profile(session, user.id)
        service.set_nickname(profile, "Night Owl")
        service.set_age(profile, 24, minimum_age=18, today=T0.date())
        service.set_gender(profile, "female")
        service.set_country(profile, "IN")
        await session.commit()
        return user.id


async def _changes(sessions: Sessions, user_id: int) -> list[tuple[str, str, str, datetime]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                ProfileChange.field,
                ProfileChange.old_value,
                ProfileChange.new_value,
                ProfileChange.changed_at,
            )
            .where(ProfileChange.user_id == user_id)
            .order_by(ProfileChange.id)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def test_first_change_is_allowed_and_recorded(
    session_factory: Sessions, user_id: int
) -> None:
    async with session_factory() as session:
        changed = await service.change_age(
            session, user_id, 25, minimum_age=18, now=T0, cooldown=COOLDOWN
        )
        await session.commit()
        profile = await service.get_profile(session, user_id)

    assert changed
    assert (profile.age_at_entry, profile.age_entered_on) == (25, T0.date())
    assert await _changes(session_factory, user_id) == [("age", "24", "25", T0)]


async def test_change_within_cooldown_is_refused_until_it_ends(
    session_factory: Sessions, user_id: int
) -> None:
    async with session_factory() as session:
        await service.change_gender(session, user_id, "male", now=T0, cooldown=COOLDOWN)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(service.ChangeCooldownError) as excinfo:
            await service.change_gender(
                session, user_id, "other", now=T0 + timedelta(days=29), cooldown=COOLDOWN
            )
        await session.rollback()
    assert excinfo.value.field == "gender"
    assert excinfo.value.available_at == T0 + COOLDOWN

    async with session_factory() as session:
        assert (await service.get_profile(session, user_id)).gender == "male"
        changed = await service.change_gender(
            session, user_id, "other", now=T0 + COOLDOWN, cooldown=COOLDOWN
        )
        await session.commit()
    assert changed
    assert [c[:3] for c in await _changes(session_factory, user_id)] == [
        ("gender", "female", "male"),
        ("gender", "male", "other"),
    ]


async def test_cooldowns_are_per_field(session_factory: Sessions, user_id: int) -> None:
    async with session_factory() as session:
        await service.change_gender(session, user_id, "male", now=T0, cooldown=COOLDOWN)
        await service.change_age(session, user_id, 30, minimum_age=18, now=T0, cooldown=COOLDOWN)
        await session.commit()

    assert [c[0] for c in await _changes(session_factory, user_id)] == ["gender", "age"]


async def test_same_value_is_not_a_change_and_does_not_start_the_cooldown(
    session_factory: Sessions, user_id: int
) -> None:
    async with session_factory() as session:
        assert not await service.change_gender(
            session, user_id, "female", now=T0, cooldown=COOLDOWN
        )
        assert not await service.change_age(
            session, user_id, 24, minimum_age=18, now=T0, cooldown=COOLDOWN
        )
        await session.commit()

    assert await _changes(session_factory, user_id) == []
    async with session_factory() as session:
        assert await service.change_available_at(session, user_id, "age", COOLDOWN) is None


async def test_age_is_compared_with_the_current_age(
    session_factory: Sessions, user_id: int
) -> None:
    # Entered as 24 at T0; a year later the displayed age is 25 (D-010).
    later = T0 + timedelta(days=366)
    async with session_factory() as session:
        assert not await service.change_age(
            session, user_id, 25, minimum_age=18, now=later, cooldown=COOLDOWN
        )
        assert await service.change_age(
            session, user_id, 26, minimum_age=18, now=later, cooldown=COOLDOWN
        )
        await session.commit()

    assert [c[:3] for c in await _changes(session_factory, user_id)] == [("age", "25", "26")]


async def test_invalid_values_write_nothing(session_factory: Sessions, user_id: int) -> None:
    async with session_factory() as session:
        with pytest.raises(ProfileValidationError):
            await service.change_age(
                session, user_id, 16, minimum_age=18, now=T0, cooldown=COOLDOWN
            )
        with pytest.raises(ProfileValidationError):
            await service.change_gender(session, user_id, "robot", now=T0, cooldown=COOLDOWN)
        await session.rollback()

    assert await _changes(session_factory, user_id) == []


async def test_concurrent_changes_cannot_both_pass_the_cooldown(
    session_factory: Sessions, user_id: int
) -> None:
    async def attempt(value: str) -> str:
        async with session_factory() as session:
            try:
                await service.change_gender(session, user_id, value, now=T0, cooldown=COOLDOWN)
                # Hold the row lock briefly so the other attempt has to wait for it.
                await asyncio.sleep(0.05)
                await session.commit()
                return "changed"
            except service.ChangeCooldownError:
                await session.rollback()
                return "cooldown"

    results = await asyncio.gather(attempt("male"), attempt("other"))

    assert sorted(results) == ["changed", "cooldown"]
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ProfileChange))
    assert count == 1
