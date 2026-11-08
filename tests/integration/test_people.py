"""People: past chat partners (D-051), and RealChat activity."""

from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import people as pp
from apps.bot.db import ACTIVITY_INTERVAL
from apps.bot.endings import BLOCK_BUTTON, LIKE_BUTTON, REPORT_BUTTON, UNBLOCK_BUTTON
from apps.bot.keyboards import END_CHAT
from apps.bot.people import PeopleAction
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.chat import service as chat_service
from backend.modules.chat.models import Conversation
from backend.modules.people import service as people
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.world import (
    BOB_ID,
    MESSAGE,
    REQUEST,
    World,
    buttons,
    last_to,
    match,
    onboarded_world,
    own_buttons,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
CAROL_ID = 6464
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """People reads the time at NOW, so "today" and the window never depend on when the
    tests run; chats are moved to end an hour before it."""
    monkeypatch.setattr(pp, "_now", lambda: NOW)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    return await onboarded_world(bot_settings, redis, session_factory)


async def chatted(world: World) -> int:
    """Alice and Bob chatted and Alice ended it; returns the conversation's id."""
    await match(world)
    await world.alice.send(END_CHAT)
    conversation_id = await latest_conversation(world.sessions)
    await end_conversation_at(world.sessions, conversation_id, NOW - timedelta(hours=1))
    return conversation_id


async def latest_conversation(sessions: Sessions) -> int:
    async with sessions() as session:
        latest = await session.scalar(select(Conversation.id).order_by(Conversation.id.desc()))
        assert latest is not None
        return int(latest)


async def open_past_chats(chat: Chat) -> str:
    await chat.send("/profile")
    assert await chat.press(pp.PEOPLE_BUTTON) == pp.PEOPLE_PROMPT
    return await chat.press(pp.PAST_CHATS_BUTTON)


def entries(chat: Chat) -> list[str]:
    return buttons(last_to(chat.session, chat.user_id))


async def end_conversation_at(sessions: Sessions, conversation_id: int, ended_at: datetime) -> None:
    async with sessions() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(started_at=ended_at - timedelta(minutes=5), ended_at=ended_at)
        )
        await session.commit()


async def set_setting(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        session.add(AppSetting(key=key.value, value=value))
        await session.commit()


def open_data(conversation_id: int) -> str:
    return PeopleAction(action="open", conversation=conversation_id).pack()


# --- Through the bot -------------------------------------------------------------------


async def test_people_is_on_the_own_profile_card(world: World) -> None:
    await world.alice.send("/profile")
    assert pp.PEOPLE_BUTTON in entries(world.alice)


async def test_a_past_partner_is_listed_and_their_card_opens(world: World) -> None:
    conversation_id = await chatted(world)
    assert await open_past_chats(world.alice) == "💬 Past chats from the last 10 days"
    assert entries(world.alice) == ["Moon Child · today"]
    await world.alice.press("Moon Child · today")
    card = last_to(world.alice.session, USER_ID)
    assert text_of(card).startswith("💬 Past chat\n\n👤 Moon Child")
    assert buttons(card) == [LIKE_BUTTON, MESSAGE, REQUEST, BLOCK_BUTTON, REPORT_BUTTON]
    assert own_buttons(world.alice)[BLOCK_BUTTON].endswith(f":{conversation_id}::0")


async def test_the_partner_sees_it_too(world: World) -> None:
    await chatted(world)
    await open_past_chats(world.bob)
    assert entries(world.bob) == [f"{await nickname(world.sessions, USER_ID)} · today"]


async def nickname(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return str((await users.get_profile(session, user.id)).nickname)


async def test_nobody_is_listed_before_any_chat_or_during_one(world: World) -> None:
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."
    await match(world)
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."


async def test_someone_being_chatted_with_again_is_left_out_until_that_chat_ends(
    world: World,
) -> None:
    await chatted(world)
    await match(world)
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."
    await world.bob.send(END_CHAT)
    again = await latest_conversation(world.sessions)
    await end_conversation_at(world.sessions, again, NOW - timedelta(minutes=5))
    await open_past_chats(world.alice)
    assert entries(world.alice) == ["Moon Child · today"]


async def test_each_partner_is_listed_once_with_their_latest_chat(
    world: World, session_factory: Sessions
) -> None:
    first = await chatted(world)
    second = await chatted(world)
    await end_conversation_at(session_factory, first, NOW - timedelta(days=3))
    await open_past_chats(world.alice)
    assert entries(world.alice) == ["Moon Child · today"]
    assert world.alice.session.last_buttons()["Moon Child · today"] == open_data(second)


async def test_chats_outside_the_window_are_not_listed_or_opened(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await chatted(world)
    await open_past_chats(world.alice)
    stale = world.alice.session.last_buttons()["Moon Child · today"]
    await end_conversation_at(session_factory, conversation_id, NOW - timedelta(days=10, minutes=1))
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."
    assert await world.alice.press_data_reply(stale) == ""
    assert world.alice.session.callback_answers()[-1] == pp.NOT_AVAILABLE


async def test_the_window_is_an_admin_setting(world: World, session_factory: Sessions) -> None:
    conversation_id = await chatted(world)
    await end_conversation_at(session_factory, conversation_id, NOW - timedelta(days=12))
    await set_setting(session_factory, SettingKey.PAST_CHATS_DAYS, 13)
    assert await open_past_chats(world.alice) == "💬 Past chats from the last 13 days"
    assert entries(world.alice) == ["Moon Child · 12 days ago"]
    await world.alice.press("Moon Child · 12 days ago")
    assert text_of(last_to(world.alice.session, USER_ID)).startswith("💬 Past chat")


async def test_a_blocked_partner_stays_listed_marked_only_for_the_blocker(world: World) -> None:
    await chatted(world)
    await open_past_chats(world.alice)
    await world.alice.press("Moon Child · today")
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    await open_past_chats(world.alice)
    assert entries(world.alice) == ["Moon Child · today 🚫"]
    await world.alice.press("Moon Child · today 🚫")
    assert buttons(last_to(world.alice.session, USER_ID)) == [
        LIKE_BUTTON,
        MESSAGE,
        REQUEST,
        UNBLOCK_BUTTON,
        REPORT_BUTTON,
    ]
    await world.alice.press_data(own_buttons(world.alice)[UNBLOCK_BUTTON])
    await open_past_chats(world.alice)
    assert entries(world.alice) == ["Moon Child · today"]
    await world.alice.press("Moon Child · today")
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    await open_past_chats(world.bob)
    assert len(entries(world.bob)) == 1
    assert not entries(world.bob)[0].endswith(pp.BLOCKED_MARK)


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_restricted_partners_are_not_listed_or_opened(
    world: World, session_factory: Sessions, status: str
) -> None:
    conversation_id = await chatted(world)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == BOB_ID).values(status=status)
        )
        await session.commit()
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."
    await world.alice.press_data(open_data(conversation_id))
    assert world.alice.session.callback_answers()[-1] == pp.NOT_AVAILABLE


async def test_chats_from_before_an_account_deletion_are_never_listed(
    world: World, session_factory: Sessions
) -> None:
    conversation_id = await chatted(world)
    bob = await world.user_id(BOB_ID)
    async with session_factory() as session:
        await users.delete_account(session, bob, NOW)
        await session.commit()
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."
    # Bob comes back as a new person: the chat from before is still not his.
    await complete_onboarding(world.bob)
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."
    assert await open_past_chats(world.bob) == "No past chats from the last 10 days."
    await world.alice.press_data(open_data(conversation_id))
    assert world.alice.session.callback_answers()[-1] == pp.NOT_AVAILABLE


async def test_a_deleted_viewer_starts_afresh(world: World, session_factory: Sessions) -> None:
    await chatted(world)
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        await users.delete_account(session, alice, NOW)
        await session.commit()
    await complete_onboarding(world.alice)
    assert await open_past_chats(world.alice) == "No past chats from the last 10 days."


async def test_someone_else_cannot_open_a_chat_they_were_not_in(
    world: World, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    conversation_id = await chatted(world)
    carol = Chat(bot_settings, redis, session_factory, user_id=CAROL_ID)
    await complete_onboarding(carol)
    await carol.press_data(open_data(conversation_id))
    assert carol.session.callback_answers()[-1] == pp.NOT_AVAILABLE
    await carol.press_data(open_data(999_999))
    assert carol.session.callback_answers()[-1] == pp.NOT_AVAILABLE


async def test_people_needs_a_finished_onboarding(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    newcomer = Chat(bot_settings, redis, session_factory)
    await newcomer.send("/start")
    terms = newcomer.last()
    await newcomer.press_data(PeopleAction(action="menu").pack())
    assert newcomer.last() == terms
    await newcomer.press_data(PeopleAction(action="past").pack())
    assert newcomer.last() == terms


# --- Pages -----------------------------------------------------------------------------


async def make_partner(session: AsyncSession, telegram_id: int, nickname: str) -> int:
    user = await users.get_or_create_user(session, telegram_id)
    await session.execute(
        update(Profile)
        .where(Profile.user_id == user.id)
        .values(
            nickname=nickname,
            age_at_entry=25,
            age_entered_on=NOW.date(),
            gender="female",
            country_code="IN",
            onboarding_completed_at=NOW,
        )
    )
    return user.id


async def test_past_chats_come_in_pages_latest_first(session_factory: Sessions) -> None:
    async with session_factory() as session:
        viewer = await make_partner(session, 1, "Viewer")
        for n in range(people.PAGE_SIZE + 1):
            partner = await make_partner(session, 100 + n, f"P{n}")
            conversation = await chat_service.start_conversation(session, viewer, partner)
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation)
                .values(started_at=NOW - timedelta(hours=n + 1))
            )
            await chat_service.end_conversation(
                session, viewer, NOW - timedelta(hours=n), conversation_id=conversation
            )
        await session.commit()
        since = people.window_start(NOW, 10)
        first = await people.past_chats(session, viewer, since)
        second = await people.past_chats(session, viewer, since, page=1)
    assert [entry.nickname for entry in first.entries] == [f"P{n}" for n in range(10)]
    assert first.has_next
    assert [entry.nickname for entry in second.entries] == ["P10"]
    assert not second.has_next


def test_page_buttons() -> None:
    found = people.PastChats(entries=[], has_next=True)
    keyboard = pp.past_chats_keyboard(1, found, NOW)
    assert [b.text for b in keyboard.inline_keyboard[0]] == [pp.PREVIOUS_BUTTON, pp.NEXT_BUTTON]
    assert pp.past_chats_keyboard(0, people.PastChats([], False), NOW).inline_keyboard == []


def test_ago() -> None:
    assert pp.ago(NOW - timedelta(minutes=5), NOW) == "today"
    assert pp.ago(NOW - timedelta(days=1), NOW) == "yesterday"
    assert pp.ago(NOW - timedelta(days=4), NOW) == "4 days ago"


def test_window_start() -> None:
    assert people.window_start(NOW, 10) == NOW - timedelta(days=10)


# --- RealChat activity -------------------------------------------------------------------


async def last_active(sessions: Sessions, telegram_id: int) -> datetime:
    async with sessions() as session:
        at = await session.scalar(
            select(User.last_active_at).where(User.telegram_user_id == telegram_id)
        )
        assert at is not None
        return at


async def set_last_active(sessions: Sessions, telegram_id: int, at: datetime) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == telegram_id).values(last_active_at=at)
        )
        await session.commit()


async def test_activity_is_recorded_at_most_once_per_interval(session_factory: Sessions) -> None:
    async with session_factory() as session:
        await users.get_or_create_user(session, USER_ID)
        await session.commit()
    await set_last_active(session_factory, USER_ID, NOW - ACTIVITY_INTERVAL)
    async with session_factory() as session:
        await users.record_activity(session, USER_ID, NOW, ACTIVITY_INTERVAL)
        await session.commit()
    assert await last_active(session_factory, USER_ID) == NOW
    later = NOW + ACTIVITY_INTERVAL - timedelta(seconds=1)
    async with session_factory() as session:
        await users.record_activity(session, USER_ID, later, ACTIVITY_INTERVAL)
        await session.commit()
    assert await last_active(session_factory, USER_ID) == NOW


async def test_using_the_bot_records_activity(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    long_ago = datetime.now(UTC) - timedelta(days=1)
    await set_last_active(session_factory, USER_ID, long_ago)
    await chat.send("/help")
    assert await last_active(session_factory, USER_ID) > long_ago


async def test_restricted_users_are_not_counted_as_active(
    bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    chat = Chat(bot_settings, redis, session_factory)
    await complete_onboarding(chat)
    long_ago = datetime.now(UTC) - timedelta(days=1)
    await set_last_active(session_factory, USER_ID, long_ago)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status="suspended")
        )
        await session.commit()
    await chat.send("/help")
    await chat.send("/profile")
    assert await last_active(session_factory, USER_ID) == long_ago


async def test_only_active_accounts_have_activity_recorded(session_factory: Sessions) -> None:
    async with session_factory() as session:
        await users.get_or_create_user(session, USER_ID)
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status="suspended")
        )
        await session.commit()
    await set_last_active(session_factory, USER_ID, NOW - timedelta(days=1))
    async with session_factory() as session:
        await users.record_activity(session, USER_ID, NOW, ACTIVITY_INTERVAL)
        await session.commit()
    assert await last_active(session_factory, USER_ID) == NOW - timedelta(days=1)
