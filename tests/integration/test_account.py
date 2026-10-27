"""Account deletion and the access gate for restricted users (D-016, D-025, D-026)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import onboarding as ob
from apps.bot import profile as pr
from apps.bot.menu import HELP_TEXT
from backend.modules.users import service
from backend.modules.users.models import Profile, ProfileChange, TermsAcceptance, User
from backend.modules.users.terms import TERMS_VERSION
from tests.support.chat import (
    TERMS,
    USER_ID,
    Chat,
    complete_onboarding,
    complete_required_steps,
    load_profile,
)
from tests.support.telegram import message_update

Sessions = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


async def _user(sessions: Sessions, telegram_id: int = USER_ID) -> User:
    async with sessions() as session:
        user = (
            await session.execute(select(User).where(User.telegram_user_id == telegram_id))
        ).scalar_one()
        return user


async def _set_status(sessions: Sessions, status: str) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status=status)
        )
        await session.commit()


async def _count(sessions: Sessions, model: type[ProfileChange] | type[TermsAcceptance]) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


# --- Service ---------------------------------------------------------------------------


@pytest.fixture
async def full_user_id(session_factory: Sessions) -> int:
    """A user with every profile field set, accepted terms, and a change history row."""
    async with session_factory() as session:
        user = await service.get_or_create_user(session, USER_ID)
        profile = await service.get_profile(session, user.id)
        service.set_nickname(profile, "Night Owl")
        service.set_age(profile, 24, minimum_age=18, today=NOW.date())
        service.set_gender(profile, "female")
        service.set_country(profile, "IN")
        service.set_city(profile, "Hyderabad")
        service.set_bio(profile, "Night walks")
        service.set_photo(profile, "AgAC-photo")
        profile.onboarding_completed_at = NOW
        await service.accept_terms(session, user.id, TERMS_VERSION)
        session.add(
            ProfileChange(
                user_id=user.id,
                field="gender",
                old_value="male",
                new_value="female",
                changed_at=NOW,
            )
        )
        await session.commit()
        return user.id


async def test_delete_account_erases_personal_data_and_keeps_a_minimal_record(
    session_factory: Sessions, full_user_id: int
) -> None:
    async with session_factory() as session:
        await service.delete_account(session, full_user_id, NOW)
        await session.commit()

    async with session_factory() as session:
        profile = await service.get_profile(session, full_user_id)
    erased = (
        profile.nickname,
        profile.age_at_entry,
        profile.age_entered_on,
        profile.gender,
        profile.country_code,
        profile.city,
        profile.bio,
        profile.photo_file_id,
        profile.onboarding_completed_at,
    )
    assert erased == (None,) * len(erased)
    assert profile.visibility == "visible"
    assert await _count(session_factory, ProfileChange) == 0
    assert await _count(session_factory, TermsAcceptance) == 0

    user = await _user(session_factory)
    assert (user.id, user.status, user.deleted_at) == (full_user_id, "deleted", NOW)


async def test_deleting_a_restricted_account_keeps_the_restriction(
    session_factory: Sessions, full_user_id: int
) -> None:
    await _set_status(session_factory, "banned")

    async with session_factory() as session:
        await service.delete_account(session, full_user_id, NOW)
        await session.commit()

    user = await _user(session_factory)
    assert (user.status, user.deleted_at) == ("banned", NOW)
    assert (await load_profile(session_factory)).nickname is None


async def test_accepting_terms_reactivates_only_a_deleted_account(
    session_factory: Sessions, full_user_id: int
) -> None:
    async with session_factory() as session:
        await service.delete_account(session, full_user_id, NOW)
        await service.accept_terms(session, full_user_id, TERMS_VERSION)
        await session.commit()
    assert (await _user(session_factory)).status == "active"

    await _set_status(session_factory, "banned")
    async with session_factory() as session:
        await service.accept_terms(session, full_user_id, TERMS_VERSION)
        await session.commit()
    assert (await _user(session_factory)).status == "banned"


# --- Bot: deletion ---------------------------------------------------------------------


async def _open_delete_confirmation(chat: Chat) -> None:
    await chat.send("/profile")
    assert await chat.press(ac.ADVANCED_SETTINGS) == ac.ADVANCED_SETTINGS_TEXT
    assert await chat.press(ac.DELETE_BUTTON) == ac.DELETE_CONFIRM


async def test_delete_account_from_the_profile_and_start_again(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await _open_delete_confirmation(chat)
    confirm = chat.session.last_buttons()[ac.CONFIRM_BUTTON]

    assert await chat.press(ac.CONFIRM_BUTTON) == ac.DELETED
    assert (await load_profile(session_factory)).nickname is None
    assert (await _user(session_factory)).status == "deleted"
    assert await _count(session_factory, TermsAcceptance) == 0

    await chat.press_data(confirm)  # repeated press
    assert chat.session.callback_answers()[-1] == ac.EXPIRED

    # A returning user starts a fresh onboarding, terms included.
    await complete_required_steps(chat)
    assert (await _user(session_factory)).status == "active"
    assert (await load_profile(session_factory)).nickname == "Night Owl"


async def test_cancelled_or_superseded_confirmations_delete_nothing(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await _open_delete_confirmation(chat)
    first_confirm = chat.session.last_buttons()[ac.CONFIRM_BUTTON]

    # Asking again replaces the first request.
    await chat.press_data(ac.AccountAction(action="delete").pack())
    await chat.press_data(first_confirm)
    assert chat.session.callback_answers()[-1] == ac.EXPIRED

    second_confirm = chat.session.last_buttons()[ac.CONFIRM_BUTTON]
    assert await chat.press(ac.KEEP_BUTTON) == ac.KEPT
    await chat.press_data(second_confirm)
    assert chat.session.callback_answers()[-1] == ac.EXPIRED

    assert (await load_profile(session_factory)).nickname == "Night Owl"
    assert (await _user(session_factory)).status == "active"


# --- Bot: access gate ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_restricted_users_are_stopped_but_keep_help_and_deletion(
    chat: Chat, session_factory: Sessions, status: str
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")
    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.NICKNAME])  # an edit in progress
    await _set_status(session_factory, status)
    notice = ac.restriction_notice(status)

    assert await chat.send("Moon Child") == notice
    assert await chat.send("/profile") == notice
    assert await chat.send("/start") == notice
    await chat.press_data(pr.EditField(field=pr.ProfileField.BIO).pack())
    assert chat.last() == notice
    await chat.press_data(ac.AccountAction(action="settings").pack())
    assert chat.last() == notice
    assert (await load_profile(session_factory)).nickname == "Night Owl"

    assert await chat.send("/help") == HELP_TEXT

    # The notice offers deletion, which erases the profile but keeps the restriction.
    await chat.send("/profile")
    assert await chat.press(ac.DELETE_BUTTON) == ac.DELETE_CONFIRM
    assert await chat.press(ac.CONFIRM_BUTTON) == ac.DELETED_RESTRICTED
    assert (await load_profile(session_factory)).nickname is None
    assert (await _user(session_factory)).status == status

    # Starting again is not possible while restricted.
    assert await chat.send("/start") == notice


async def test_unrestricted_users_are_not_affected(chat: Chat) -> None:
    assert await chat.send("/start") == TERMS
    assert await chat.press("I agree") == ob.PROMPTS["nickname"]


async def test_restricted_users_get_no_notice_in_groups(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await _set_status(session_factory, "banned")
    before = len(chat.session.sent_texts())

    await chat.dispatcher.feed_update(
        chat.bot, message_update(9401, USER_ID, "/profile", chat_type="group")
    )

    assert len(chat.session.sent_texts()) == before


async def test_profile_rows_are_never_duplicated_by_deletion(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await _open_delete_confirmation(chat)
    await chat.press(ac.CONFIRM_BUTTON)

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Profile)) == 1
