"""Blocking and reporting chat partners (D-037, D-045)."""

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import chat as ch
from apps.bot import safety as sf
from apps.bot.endings import (
    BLOCK_BUTTON,
    MEET_PROMPT,
    PARTNER_ENDED,
    REPORT_BUTTON,
    UNBLOCK_BUTTON,
    SafetyAction,
)
from apps.bot.keyboards import END_CHAT, PARTNER_PROFILE
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.chat import service as chat_service
from backend.modules.chat.models import Conversation
from backend.modules.moderation import service as moderation
from backend.modules.moderation.models import Report, UserBlock
from backend.modules.moderation.service import NotPartnerError, ReportOutcome
from backend.modules.users import service as users
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked
from tests.support.telegram import RecordingSession, photo_update
from tests.support.world import (
    BOB_ID,
    MAIN_KEYS,
    World,
    assert_chat_ended,
    buttons,
    keys,
    last_to,
    match,
    onboarded_world,
    search,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
CAROL_ID = 6464
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    return await onboarded_world(bot_settings, redis, session_factory)


@pytest.fixture
async def carol(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    carol = Chat(bot_settings, redis, session_factory, user_id=CAROL_ID)
    await complete_onboarding(carol)
    return carol


def data(action: str, conversation: int, **fields: str | int) -> str:
    return SafetyAction(action=action, conversation=conversation, **fields).pack()


async def conversations(sessions: Sessions) -> list[Conversation]:
    async with sessions() as session:
        return list(
            (await session.execute(select(Conversation).order_by(Conversation.id))).scalars()
        )


async def blocks(sessions: Sessions) -> list[UserBlock]:
    async with sessions() as session:
        return list((await session.execute(select(UserBlock).order_by(UserBlock.id))).scalars())


async def reports(sessions: Sessions) -> list[Report]:
    async with sessions() as session:
        return list((await session.execute(select(Report).order_by(Report.id))).scalars())


def last_edit(session: RecordingSession) -> EditMessageReplyMarkup:
    return [m for m in session.requests if isinstance(m, EditMessageReplyMarkup)][-1]


def edited_buttons(session: RecordingSession) -> list[str]:
    markup = last_edit(session).reply_markup
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


async def matched(world: World) -> int:
    """Match Alice and Bob; returns their conversation's id."""
    await match(world)
    return (await conversations(world.sessions))[-1].id


async def ended_chat(world: World) -> int:
    """Alice and Bob chatted and Alice ended it; returns the conversation's id."""
    conversation_id = await matched(world)
    await world.alice.send(END_CHAT)
    return conversation_id


# --- The partner card ------------------------------------------------------------------


async def test_the_partner_card_offers_block_and_report(world: World) -> None:
    await matched(world)
    await world.alice.send(PARTNER_PROFILE)
    card = last_to(world.alice.session, USER_ID)
    assert text_of(card).startswith("Your chat partner:\n\n👤 Moon Child")
    assert buttons(card) == [BLOCK_BUTTON, REPORT_BUTTON]


# --- Blocking --------------------------------------------------------------------------


async def test_blocking_during_a_chat_is_confirmed_then_ends_it(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await matched(world)
    await world.alice.send(PARTNER_PROFILE)
    assert await world.alice.press(BLOCK_BUTTON) == sf.CONFIRM_BLOCK
    assert await blocks(session_factory) == []  # nothing happens before confirming

    await world.alice.press(sf.CONFIRM_YES)
    assert_chat_ended(world.alice.session, USER_ID, sf.YOU_BLOCKED, blocked=True)
    # Bob is only told the chat ended, and is offered Block himself.
    assert_chat_ended(world.alice.session, BOB_ID, PARTNER_ENDED, blocked=False)
    assert last_edit(world.alice.session).reply_markup is None  # the confirmation is used up

    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    (block,) = await blocks(session_factory)
    assert (block.blocker_user_id, block.blocked_user_id, block.conversation_id) == (
        alice,
        bob,
        conversation_id,
    )
    assert block.unblocked_at is None
    (conversation,) = await conversations(session_factory)
    assert (conversation.end_reason, conversation.ended_by_user_id) == ("blocked", alice)


async def test_cancelling_the_confirmation_keeps_the_chat(
    world: World, session_factory: Sessions
) -> None:
    await matched(world)
    await world.alice.send(PARTNER_PROFILE)
    await world.alice.press(BLOCK_BUTTON)
    await world.alice.press(sf.CONFIRM_NO)
    assert world.alice.session.callback_answers()[-1] == sf.NOT_BLOCKED
    assert await blocks(session_factory) == []
    assert (await conversations(session_factory))[0].ended_at is None


async def test_blocking_after_a_chat_takes_one_tap_and_toggles(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await ended_chat(world)
    before = len(world.alice.session.sent_texts())
    await world.alice.press_data(data("block", conversation_id))
    assert world.alice.session.sent_texts()[before:] == []  # no confirmation, no messages
    assert world.alice.session.callback_answers()[-1] == sf.BLOCKED
    assert edited_buttons(world.alice.session) == [UNBLOCK_BUTTON, REPORT_BUTTON]
    assert len(await blocks(session_factory)) == 1

    await world.alice.press_data(data("unblock", conversation_id))
    assert world.alice.session.callback_answers()[-1] == sf.UNBLOCKED
    assert edited_buttons(world.alice.session) == [BLOCK_BUTTON, REPORT_BUTTON]
    (block,) = await blocks(session_factory)
    assert block.unblocked_at is not None  # kept as history

    await world.alice.press_data(data("block", conversation_id))
    history = await blocks(session_factory)
    assert [b.unblocked_at is None for b in history] == [False, True]


async def test_repeated_block_presses_store_one_block_and_end_once(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await matched(world)
    await world.alice.press_data(data("confirm", conversation_id))
    told = len(world.alice.session.sent_to(BOB_ID))
    await world.alice.press_data(data("confirm", conversation_id))
    await world.alice.press_data(data("block", conversation_id))
    assert world.alice.session.callback_answers()[-2:] == [sf.ALREADY_BLOCKED] * 2
    assert len(world.alice.session.sent_to(BOB_ID)) == told  # Bob was told once
    assert len(await blocks(session_factory)) == 1


async def test_a_stale_confirmation_never_ends_a_newer_chat(
    world: World, session_factory: Sessions
) -> None:
    first = await matched(world)
    await world.alice.send(PARTNER_PROFILE)
    await world.alice.press(BLOCK_BUTTON)  # asks, but Alice ends the chat instead
    await world.alice.send(END_CHAT)
    await world.alice.press_data(data("unblock", first))
    second = await matched(world)
    await world.alice.press_data(data("confirm", first))  # the old confirmation
    assert world.alice.session.callback_answers()[-1] == sf.BLOCKED
    chats = await conversations(session_factory)
    assert [c.id for c in chats] == [first, second]
    assert chats[1].ended_at is None


async def test_blocked_partners_can_still_be_matched(
    world: World, session_factory: Sessions
) -> None:
    # Blocks never steer random matching, so they cannot be used to pick partners.
    conversation_id = await ended_chat(world)
    await world.alice.press_data(data("block", conversation_id))
    await matched(world)
    chats = await conversations(session_factory)
    assert len(chats) == 2 and chats[1].ended_at is None
    # Alice's card for Bob in the new chat shows that he is blocked.
    await world.alice.send(PARTNER_PROFILE)
    assert buttons(last_to(world.alice.session, USER_ID)) == [UNBLOCK_BUTTON, REPORT_BUTTON]


# --- Reporting -------------------------------------------------------------------------


async def test_reporting_during_a_chat_files_blocks_and_ends_it(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await matched(world)
    await world.alice.send(PARTNER_PROFILE)
    assert await world.alice.press(REPORT_BUTTON) == sf.REPORT_PROMPT
    assert list(world.alice.session.last_buttons()) == list(sf.REASON_LABELS.values())

    assert await world.alice.press("Harassment or hate") == sf.DETAILS_PROMPT
    texts = world.alice.session.sent_texts()
    assert texts[-2] == sf.REPORT_SENT_ENDED
    assert keys(world.alice.session.sent_to(USER_ID)[-2]) == MAIN_KEYS
    assert_chat_ended(world.alice.session, BOB_ID, PARTNER_ENDED)

    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    (report,) = await reports(session_factory)
    assert (report.reporter_user_id, report.reported_user_id, report.conversation_id) == (
        alice,
        bob,
        conversation_id,
    )
    assert (report.reason, report.urgent, report.details, report.status) == (
        "harassment",
        False,
        None,
        "open",
    )
    (block,) = await blocks(session_factory)
    assert (block.blocker_user_id, block.blocked_user_id) == (alice, bob)
    (conversation,) = await conversations(session_factory)
    assert (conversation.end_reason, conversation.ended_by_user_id) == ("reported", alice)

    # Details are added to the report, then the new-chat menu follows.
    assert await world.alice.send("  He kept insulting me.  ") == (
        f"{sf.DETAILS_ADDED}\n\n{MEET_PROMPT}"
    )
    assert (await reports(session_factory))[0].details == "He kept insulting me."
    # The next message is no longer taken as details.
    assert await world.alice.send("hello") != sf.DETAILS_ADDED
    assert (await reports(session_factory))[0].details == "He kept insulting me."


async def test_appears_under_18_is_urgent(world: World, session_factory: Sessions) -> None:
    conversation_id = await ended_chat(world)
    await world.alice.press_data(data("report", conversation_id))
    await world.alice.press("Appears under 18")
    assert world.alice.session.sent_texts()[-2] == sf.REPORT_SENT
    (report,) = await reports(session_factory)
    assert (report.reason, report.urgent) == ("under_18", True)


async def test_details_can_be_skipped(world: World, session_factory: Sessions) -> None:
    conversation_id = await ended_chat(world)
    await world.alice.press_data(data("report", conversation_id))
    await world.alice.press("Spam or scam")
    assert await world.alice.press(sf.SKIP_BUTTON) == sf.DETAILS_SKIPPED
    assert (await reports(session_factory))[0].details is None
    # An old Skip button, or one pressed twice, does nothing more.
    await world.alice.press_data(
        data("skip", conversation_id, report=(await reports(session_factory))[0].id)
    )
    assert world.alice.session.callback_answers()[-1] == sf.DETAILS_DONE


async def test_details_must_be_text_of_at_most_500_characters(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await ended_chat(world)
    await world.alice.press_data(data("report", conversation_id))
    await world.alice.press("Other")
    assert await world.alice.send("x" * 501) == sf.DETAILS_TOO_LONG
    await world.alice.dispatcher.feed_update(world.alice.bot, photo_update(90_001, USER_ID, "p"))
    assert world.alice.last() == sf.DETAILS_TEXT_ONLY
    assert (await reports(session_factory))[0].details is None
    assert await world.alice.send("y" * 500) == sf.DETAILS_ADDED
    assert (await reports(session_factory))[0].details == "y" * 500


async def test_a_chat_is_reported_once(world: World, session_factory: Sessions) -> None:
    conversation_id = await ended_chat(world)
    await world.alice.press_data(data("report", conversation_id))
    reasons = world.alice.session.last_buttons()
    await world.alice.press("Spam or scam")
    await world.alice.press(sf.SKIP_BUTTON)
    # A second Report tap, or an old reason button, files nothing.
    await world.alice.press_data(data("report", conversation_id))
    assert world.alice.session.callback_answers()[-1] == sf.ALREADY_REPORTED
    await world.alice.press_data(reasons["Threats or violence"])
    assert world.alice.session.callback_answers()[-1] == sf.ALREADY_REPORTED
    assert len(await reports(session_factory)) == 1


async def test_reports_are_limited_per_day_but_blocking_is_not(
    world: World, session_factory: Sessions
) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.REPORTS_PER_DAY.value, value=1))
        await session.commit()
    first = await ended_chat(world)
    await world.alice.press_data(data("reason", first, reason="spam"))
    await world.alice.press(sf.SKIP_BUTTON)
    second = await ended_chat(world)
    await world.alice.press_data(data("reason", second, reason="spam"))
    assert world.alice.session.callback_answers()[-1] == sf.LIMIT_REACHED
    assert [r.conversation_id for r in await reports(session_factory)] == [first]
    await world.alice.press_data(data("unblock", second))
    await world.alice.press_data(data("block", second))
    assert world.alice.session.callback_answers()[-1] == sf.BLOCKED


async def test_reporting_an_old_chat_leaves_the_current_one_alone(
    world: World, carol: Chat, session_factory: Sessions
) -> None:
    first = await ended_chat(world)
    assert await search(world.alice) == ch.SEARCHING
    assert await search(carol) == ch.SEARCHING
    await world.tick()
    await world.alice.press_data(data("reason", first, reason="spam"))
    assert world.alice.session.sent_texts()[-1] == sf.REPORT_SENT  # no details asked
    chats = await conversations(session_factory)
    assert len(chats) == 2 and chats[1].ended_at is None  # the chat with Carol goes on
    # Alice's next message goes to Carol, not into the report.
    before = len(world.alice.session.copies())
    await world.alice.send("hi Carol")
    assert len(world.alice.session.copies()) == before + 1
    assert (await reports(session_factory))[0].details is None


async def test_no_details_are_asked_for_while_searching(
    world: World, carol: Chat, session_factory: Sessions
) -> None:
    first = await ended_chat(world)
    assert await search(world.alice) == ch.SEARCHING
    await world.alice.press_data(data("reason", first, reason="spam"))
    assert world.alice.session.sent_texts()[-1] == sf.REPORT_SENT  # no details asked
    # Matched afterwards, Alice's first message goes to her new partner.
    assert await search(carol) == ch.SEARCHING
    await world.tick()
    before = len(world.alice.session.copies())
    await world.alice.send("hi Carol")
    assert len(world.alice.session.copies()) == before + 1
    assert (await reports(session_factory))[0].details is None


# --- Access ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        {"action": "block"},
        {"action": "confirm"},
        {"action": "unblock"},
        {"action": "report"},
        {"action": "reason", "reason": "spam"},
    ],
)
async def test_nobody_outside_a_chat_can_block_or_report_in_it(
    world: World, carol: Chat, session_factory: Sessions, action: dict[str, str]
) -> None:
    conversation_id = await matched(world)
    for conversation in (conversation_id, 999_999):
        await carol.press_data(SafetyAction(conversation=conversation, **action).pack())
        assert carol.telegram.callback_answers()[-1] == sf.NOT_AVAILABLE
    assert await blocks(session_factory) == []
    assert await reports(session_factory) == []
    assert (await conversations(session_factory))[0].ended_at is None


async def test_an_unknown_reason_files_nothing(world: World, session_factory: Sessions) -> None:
    conversation_id = await ended_chat(world)
    await world.alice.press_data(data("reason", conversation_id, reason="bogus"))
    assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    assert await reports(session_factory) == []


async def test_blocks_and_reports_survive_account_deletion(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await ended_chat(world)
    await world.alice.press_data(data("reason", conversation_id, reason="spam"))
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        await users.delete_account(session, alice, NOW)
        await session.commit()
    assert len(await blocks(session_factory)) == 1
    assert len(await reports(session_factory)) == 1


# --- Concurrency (service level) -------------------------------------------------------


async def test_simultaneous_reports_cannot_pass_the_limit_together(
    world: World, session_factory: Sessions
) -> None:
    first = await ended_chat(world)
    second = await ended_chat(world)
    alice = await world.user_id(USER_ID)
    async with session_factory() as holding:
        filed = await moderation.file_report(holding, alice, first, "spam", NOW, per_day=1)
        assert filed.outcome is ReportOutcome.FILED

        async def other() -> ReportOutcome:
            async with session_factory() as session:
                result = await moderation.file_report(session, alice, second, "spam", NOW, 1)
                await session.commit()
                return result.outcome

        task = asyncio.create_task(other())
        await wait_until_blocked(session_factory)
        assert not task.done()  # waits for the first report's lock on the user
        await holding.commit()
        assert await task is ReportOutcome.LIMIT_REACHED
    assert len(await reports(session_factory)) == 1


async def test_reporting_does_not_wait_on_a_report_about_the_reporter(
    world: World, session_factory: Sessions
) -> None:
    # A report key-share locks the reported user's row. Reports by that user must not
    # wait on it, or two users reporting each other at once would deadlock.
    first = await ended_chat(world)
    second = await ended_chat(world)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as holding:
        filed = await moderation.file_report(holding, alice, first, "spam", NOW, 10)
        assert filed.outcome is ReportOutcome.FILED

        async def other() -> ReportOutcome:
            async with session_factory() as session:
                result = await moderation.file_report(session, bob, second, "spam", NOW, 10)
                await session.commit()
                return result.outcome

        assert await asyncio.wait_for(other(), timeout=5) is ReportOutcome.FILED
        await holding.commit()
    assert len(await reports(session_factory)) == 2


async def test_simultaneous_blocks_store_one(world: World, session_factory: Sessions) -> None:
    conversation_id = await ended_chat(world)
    alice = await world.user_id(USER_ID)

    async def block() -> bool:
        async with session_factory() as session:
            result = await moderation.block(session, alice, conversation_id, NOW)
            await session.commit()
            return result.added

    assert sorted(await asyncio.gather(block(), block())) == [False, True]
    assert len(await blocks(session_factory)) == 1


async def test_the_service_refuses_non_participants(
    world: World, carol: Chat, session_factory: Sessions
) -> None:
    conversation_id = await matched(world)
    carol_id = await world.user_id(CAROL_ID)
    async with session_factory() as session:
        with pytest.raises(NotPartnerError):
            await moderation.block(session, carol_id, conversation_id, NOW)
        with pytest.raises(NotPartnerError):
            await moderation.file_report(session, carol_id, conversation_id, "spam", NOW, 10)
        with pytest.raises(NotPartnerError):
            await moderation.unblock(session, carol_id, conversation_id, NOW)
        assert await chat_service.active_chat(session, await world.user_id(USER_ID)) is not None


async def test_details_are_added_once_and_only_by_the_reporter(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await ended_chat(world)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        filed = await moderation.file_report(session, alice, conversation_id, "spam", NOW, 10)
        assert filed.report_id is not None
        assert not await moderation.add_details(session, bob, filed.report_id, "not mine")
        assert await moderation.add_details(session, alice, filed.report_id, "first")
        assert not await moderation.add_details(session, alice, filed.report_id, "second")
        await session.commit()
    assert (await reports(session_factory))[0].details == "first"
