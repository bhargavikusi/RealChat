"""Button actions still run when Telegram rejects a late acknowledgement (#29).

Each test turns on the rejection just before the tap, as happens when a tap reaches
the bot after Telegram's answer window has passed.
"""

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import admin as ad
from apps.bot import onboarding as ob
from apps.bot import profile as pr
from apps.bot.errors import GENERIC_ERROR_TEXT
from backend.modules.admin import service as admin_service
from backend.modules.admin.settings import load_values
from backend.modules.users.terms import terms_body
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding

Sessions = async_sessionmaker[AsyncSession]
EXPIRED = "Bad Request: query is too old and response timeout expired or query ID is invalid"


async def test_onboarding_buttons(chat: Chat) -> None:
    assert await chat.send("/start") == TERMS
    chat.session.reject_callback_answers = EXPIRED
    assert await chat.press("I agree") == ob.PROMPTS["nickname"]


async def test_help_buttons(chat: Chat) -> None:
    await chat.send("/help")
    chat.session.reject_callback_answers = EXPIRED
    assert await chat.press("📄 Terms and privacy") == terms_body(18)


async def test_profile_buttons(chat: Chat) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")
    chat.session.reject_callback_answers = EXPIRED
    assert (
        await chat.press(pr.BUTTON_LABELS[pr.ProfileField.NICKNAME])
        == pr.EDIT_PROMPTS[pr.ProfileField.NICKNAME]
    )


async def test_account_buttons(chat: Chat) -> None:
    await complete_onboarding(chat)
    await chat.send("/profile")
    chat.session.reject_callback_answers = EXPIRED
    assert await chat.press(ac.ADVANCED_SETTINGS) == ac.ADVANCED_SETTINGS_TEXT
    assert await chat.press(ac.DELETE_BUTTON) == ac.DELETE_CONFIRM


async def test_admin_buttons(chat: Chat, session_factory: Sessions) -> None:
    await complete_onboarding(chat)
    async with session_factory() as session:
        await admin_service.set_role(session, USER_ID, "admin")
        await session.commit()
        values = await load_values(session)
    await chat.send("/profile")
    await chat.press(ac.ADVANCED_SETTINGS)
    chat.session.reject_callback_answers = EXPIRED
    assert await chat.press(ad.ADMIN_BUTTON) == ad.overview(values)


async def test_expired_acknowledgements_are_logged_without_details(
    chat: Chat, caplog: pytest.LogCaptureFixture
) -> None:
    await chat.send("/help")
    chat.session.reject_callback_answers = EXPIRED
    with caplog.at_level(logging.INFO, logger="apps.bot.telegram_session"):
        await chat.press("📄 Terms and privacy")
    records = [r for r in caplog.records if r.name == "apps.bot.telegram_session"]
    assert [r.getMessage() for r in records] == [
        "callback acknowledgement expired; continuing without it"
    ]
    assert not any(hasattr(r, "update_id") or hasattr(r, "user_id") for r in records)


async def test_other_rejections_are_still_errors(
    chat: Chat, caplog: pytest.LogCaptureFixture
) -> None:
    await chat.send("/help")
    sent = len(chat.session.sent_texts())
    chat.session.reject_callback_answers = "Bad Request: something else went wrong"
    with caplog.at_level(logging.ERROR, logger="apps.bot.errors"):
        await chat.press("📄 Terms and privacy")

    # The handler stopped and the error handler ran, as before.
    assert chat.session.sent_texts()[sent:] == []
    assert any(r.getMessage() == "unhandled error while processing update" for r in caplog.records)
    assert GENERIC_ERROR_TEXT in chat.session.callback_answers()
