"""/profile and /help end to end through the simulated Telegram, with real PostgreSQL and Redis."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import GetFile
from aiogram.types import FSInputFile
from redis.asyncio import Redis
from sqlalchemy import func, select

from apps.bot import onboarding as ob
from apps.bot import profile as pr
from apps.bot.avatars import avatar_path
from apps.bot.menu import BOT_COMMANDS, HELP_TEXT
from backend.config.settings import BotSettings
from backend.modules.users.models import ProfileChange
from backend.modules.users.terms import terms_body
from backend.modules.users.validation import Gender
from tests.support.chat import (
    TERMS,
    USER_ID,
    Chat,
    Sessions,
    complete_onboarding,
    complete_required_steps,
    load_profile,
)
from tests.support.telegram import message_update

CARD = f"{pr.PROFILE_HEADER}\n\n👤 Night Owl\n🎂 24 · Female\n📍 India"


def _no_downloads(chat: Chat) -> bool:
    return not any(isinstance(m, GetFile) for m in chat.session.requests)


async def _history_count(sessions: Sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(ProfileChange)) or 0


# --- Access --------------------------------------------------------------------------


async def test_profile_needs_terms_and_finished_onboarding(chat: Chat) -> None:
    assert await chat.send("/profile") == TERMS

    await complete_required_steps(chat)
    # Still in the optional steps: the user is taken back to them.
    assert await chat.send("/profile") == ob.PROMPTS["city"]


async def test_profile_is_ignored_in_group_chats(chat: Chat) -> None:
    await complete_onboarding(chat)
    before = len(chat.session.requests)

    await chat.dispatcher.feed_update(
        chat.bot, message_update(9301, USER_ID, "/profile", chat_type="group")
    )

    assert len(chat.session.requests) == before


# --- Viewing -------------------------------------------------------------------------


async def test_profile_card_uses_the_default_avatar_for_the_gender(chat: Chat) -> None:
    await complete_onboarding(chat)

    assert await chat.send("/profile") == CARD
    first = chat.session.sent_photos()[-1]
    assert isinstance(first.photo, FSInputFile)
    assert first.photo.path == avatar_path(Gender.FEMALE)

    # The upload's file identifier is reused instead of uploading again.
    assert await chat.send("/profile") == CARD
    second = chat.session.sent_photos()[-1]
    assert isinstance(second.photo, str) and second.photo.startswith("uploaded-")
    assert set(chat.session.last_buttons()) == set(pr.BUTTON_LABELS.values())


async def test_avatar_is_uploaded_again_if_the_cache_is_lost(chat: Chat, redis: Redis) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")
    await redis.flushdb()

    await chat.send("/profile")

    assert isinstance(chat.session.sent_photos()[-1].photo, FSInputFile)


# --- Text fields ---------------------------------------------------------------------


async def test_nickname_can_be_changed_and_is_validated(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")

    assert (
        await chat.press(pr.BUTTON_LABELS[pr.ProfileField.NICKNAME])
        == pr.EDIT_PROMPTS[pr.ProfileField.NICKNAME]
    )
    reply = await chat.send("x")
    assert reply.startswith("Nickname must be 2-20 characters.")
    assert await chat.send("/settings") == ob.WELCOME_BACK  # commands are never answers
    await chat.press_data(pr.EditField(field=pr.ProfileField.NICKNAME).pack())
    assert (await chat.send("Moon Child")).startswith(pr.SAVED)

    assert (await load_profile(session_factory)).nickname == "Moon Child"


async def test_city_and_bio_can_be_set_and_removed(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.CITY])
    assert "Remove city" not in chat.session.last_buttons()  # nothing to remove yet
    assert (await chat.send("Hyderabad")).startswith(pr.SAVED)

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.BIO])
    assert (await chat.send("see www.example.com")).startswith("Bio can't contain links.")
    assert (await chat.send("Night walks and old films")).endswith("Night walks and old films")

    profile = await load_profile(session_factory)
    assert (profile.city, profile.bio) == ("Hyderabad", "Night walks and old films")
    assert "📍 Hyderabad, India" in chat.last()

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.CITY])
    remove = chat.session.last_buttons()["Remove city"]
    assert (await chat.press("Remove city")).startswith(pr.REMOVED)
    await chat.press_data(remove)  # repeated press
    assert chat.session.callback_answers()[-1] == pr.NOTHING_TO_REMOVE

    profile = await load_profile(session_factory)
    assert (profile.city, profile.bio) == (None, "Night walks and old films")


async def test_country_can_be_changed_and_stale_choices_expire(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.COUNTRY])
    assert await chat.send("germany") == ob.COUNTRY_PICK
    germany = chat.session.last_buttons()["Germany"]
    assert (await chat.press("Germany")).startswith(pr.SAVED)
    await chat.press_data(germany)  # repeated press

    assert chat.session.callback_answers()[-1] == pr.EXPIRED
    assert (await load_profile(session_factory)).country_code == "DE"


async def test_cancel_changes_nothing_and_a_stale_cancel_expires(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.NICKNAME])
    cancel = chat.session.last_buttons()["Cancel"]
    assert await chat.press("Cancel") == pr.CANCELLED
    await chat.press_data(cancel)
    assert chat.session.callback_answers()[-1] == pr.EXPIRED

    # No edit is in progress, so this text is not taken as a nickname.
    assert await chat.send("Moon Child") == ob.WELCOME_BACK
    assert (await load_profile(session_factory)).nickname == "Night Owl"


async def test_a_photo_sent_to_a_text_step_is_not_saved(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")
    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.NICKNAME])

    assert await chat.send_photo("AgAC-not-a-nickname") == pr.TEXT_EXPECTED
    assert (await load_profile(session_factory)).photo_file_id is None


# --- Age and gender (cooldown) -------------------------------------------------------


async def test_age_change_is_validated_recorded_and_then_limited(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")

    prompt = await chat.press(pr.BUTTON_LABELS[pr.ProfileField.AGE])
    assert prompt.endswith(pr.cooldown_note(pr.ProfileField.AGE, 30))
    assert (await chat.send("16")).startswith("You must be at least 18")
    assert (await chat.send("25")).startswith(pr.SAVED)
    assert (await load_profile(session_factory)).age_at_entry == 25
    assert await _history_count(session_factory) == 1

    # Within the cooldown: no edit starts, and a typed age is not taken.
    reply = await chat.press(pr.BUTTON_LABELS[pr.ProfileField.AGE])
    assert reply.startswith("You can change your age again on ")
    assert await chat.send("26") == ob.WELCOME_BACK
    assert (await load_profile(session_factory)).age_at_entry == 25


async def test_same_age_is_not_recorded(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")
    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.AGE])

    assert (await chat.send("24")).startswith(pr.unchanged(pr.ProfileField.AGE))
    assert await _history_count(session_factory) == 0


async def test_gender_change_updates_the_avatar_and_stale_buttons_expire(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.GENDER])
    other = chat.session.last_buttons()["Other"]
    assert (await chat.press("Male")).startswith(pr.SAVED)
    photo = chat.session.sent_photos()[-1].photo
    assert isinstance(photo, FSInputFile) and photo.path == avatar_path(Gender.MALE)

    await chat.press_data(other)  # a second option from the same message
    assert chat.session.callback_answers()[-1] == pr.EXPIRED
    assert (await load_profile(session_factory)).gender == "male"
    assert await _history_count(session_factory) == 1


async def test_gender_can_change_again_after_the_cooldown(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    profile = await load_profile(session_factory)
    async with session_factory() as session:
        session.add(
            ProfileChange(
                user_id=profile.user_id,
                field="gender",
                old_value="male",
                new_value="female",
                changed_at=datetime.now(UTC) - timedelta(days=31),
            )
        )
        await session.commit()
    await chat.send("/profile")

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.GENDER])
    assert (await chat.press("Other")).startswith(pr.SAVED)
    assert (await load_profile(session_factory)).gender == "other"


# --- Photo ---------------------------------------------------------------------------


async def test_photo_can_be_added_replaced_and_removed_without_downloads(
    chat: Chat, session_factory: Sessions
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.PHOTO])
    assert await chat.send("not a photo") == pr.PHOTO_EXPECTED
    assert (await chat.send_photo("AgAC-first")).startswith(pr.SAVED)
    assert chat.session.sent_photos()[-1].photo == "AgAC-first"

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.PHOTO])
    assert (await chat.send_photo("AgAC-second")).startswith(pr.SAVED)
    assert (await load_profile(session_factory)).photo_file_id == "AgAC-second"

    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.PHOTO])
    assert (await chat.press("Remove photo")).startswith(pr.REMOVED)
    assert (await load_profile(session_factory)).photo_file_id is None
    # Without a photo, the card falls back to the default avatar.
    assert chat.session.sent_photos()[-1].photo != "AgAC-second"
    assert _no_downloads(chat)


# --- Terms ---------------------------------------------------------------------------


async def test_terms_update_blocks_profile_edits(
    chat: Chat, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")
    await chat.press(pr.BUTTON_LABELS[pr.ProfileField.NICKNAME])

    monkeypatch.setattr(ob, "TERMS_VERSION", "draft-99")
    assert await chat.send("Moon Child") == TERMS
    assert (await load_profile(session_factory)).nickname == "Night Owl"


# --- Help ----------------------------------------------------------------------------


async def test_help_shows_the_guide_and_terms_without_unset_links(chat: Chat) -> None:
    assert await chat.send("/help") == HELP_TEXT
    assert set(chat.session.last_buttons()) == {"📄 Terms and privacy"}

    assert await chat.press("📄 Terms and privacy") == terms_body(18)
    assert all(f"/{c.command}" in HELP_TEXT for c in BOT_COMMANDS)


async def test_help_shows_configured_links(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    settings = bot_settings.model_copy(
        update={
            "announcement_channel_url": "https://t.me/realchat_news",
            "support_contact": "https://t.me/realchat_support",
        }
    )
    chat = Chat(settings, redis, session_factory)

    await chat.send("/help")

    buttons = chat.session.last_buttons()
    assert buttons["📢 Announcements"] == "https://t.me/realchat_news"
    assert buttons["💬 Support"] == "https://t.me/realchat_support"
