"""Profile visits and Visited you (D-053)."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import people as pp
from apps.bot.endings import BLOCK_BUTTON, REPORT_BUTTON
from apps.bot.keyboards import END_CHAT, PARTNER_PROFILE
from apps.bot.people import PeopleAction
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat.models import Conversation
from backend.modules.people import visits
from backend.modules.people.models import ProfileVisit
from backend.modules.users import service as users
from backend.modules.users.models import User
from backend.modules.users.service import Notification
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked
from tests.support.world import (
    BOB_ID,
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
ALICE = "Night Owl"
BOB = "Moon Child"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """People reads the time at NOW, so labels and windows never depend on the clock."""
    monkeypatch.setattr(pp, "_now", lambda: NOW)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    world = await onboarded_world(bot_settings, redis, session_factory)
    await match(world)
    await world.alice.send(END_CHAT)
    async with session_factory() as session:
        await session.execute(
            update(Conversation).values(
                started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1)
            )
        )
        await session.commit()
    return world


async def open_people(chat: Chat) -> None:
    await chat.send("/profile")
    assert await chat.press(pp.PEOPLE_BUTTON) == pp.PEOPLE_PROMPT


async def alice_visits_bob(world: World) -> None:
    await open_people(world.alice)
    await world.alice.press(pp.PAST_CHATS_BUTTON)
    await world.alice.press(f"{BOB} · today")


async def open_visitors(chat: Chat) -> str:
    await open_people(chat)
    return await chat.press(pp.VISITORS_BUTTON)


def entries(chat: Chat) -> list[str]:
    return buttons(last_to(chat.session, chat.user_id))


def notices_to(chat: Chat, telegram_id: int) -> list[str]:
    """Visit notices `chat`'s bot sent to `telegram_id`."""
    return [
        text_of(m)
        for m in chat.session.sent_to(telegram_id)
        if text_of(m).startswith("👀") and text_of(m).endswith("viewed your profile.")
    ]


async def visit_rows(sessions: Sessions) -> list[tuple[int, int, datetime]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                ProfileVisit.visitor_user_id,
                ProfileVisit.visited_user_id,
                ProfileVisit.last_visited_at,
            )
        )
        return [(int(a), int(b), at) for a, b, at in rows]


async def set_setting(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        session.add(AppSetting(key=key.value, value=value))
        await session.commit()


async def move_visits(sessions: Sessions, at: datetime) -> None:
    async with sessions() as session:
        await session.execute(update(ProfileVisit).values(last_visited_at=at))
        await session.commit()


def visitor_data(user_id: int) -> str:
    return PeopleAction(action="visitor", user=user_id).pack()


# --- Recording and notices ----------------------------------------------------------------


async def test_opening_a_past_partner_records_a_visit_and_tells_them(
    world: World, session_factory: Sessions
) -> None:
    await alice_visits_bob(world)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    assert await visit_rows(session_factory) == [(alice, bob, NOW)]
    notice = last_to(world.alice.session, BOB_ID)
    assert text_of(notice) == f"👀 {ALICE} viewed your profile."
    assert buttons(notice) == [pp.VIEW_PROFILE_BUTTON]


async def test_visited_you_lists_the_visitor_whose_card_opens(world: World) -> None:
    await alice_visits_bob(world)
    assert await open_visitors(world.bob) == "👀 Visited you in the last 30 days"
    assert entries(world.bob) == [f"{ALICE} · today"]
    await world.bob.press(f"{ALICE} · today")
    card = last_to(world.bob.session, BOB_ID)
    assert text_of(card).startswith(f"{pp.VISITOR_HEADER}\n\n👤 {ALICE}")
    assert buttons(card) == [BLOCK_BUTTON, REPORT_BUTTON]
    # Opening a visitor's card is a visit too, so Alice is told.
    assert notices_to(world.bob, USER_ID) == [f"👀 {BOB} viewed your profile."]


async def test_the_notice_button_opens_the_visitors_card(world: World) -> None:
    await alice_visits_bob(world)
    data = world.alice.session.last_buttons()[pp.VIEW_PROFILE_BUTTON]
    await world.bob.press_data(data)
    assert text_of(last_to(world.bob.session, BOB_ID)).startswith(pp.VISITOR_HEADER)


async def test_every_visit_notifies_but_the_visitor_is_listed_once(
    world: World, session_factory: Sessions
) -> None:
    await open_people(world.alice)
    await world.alice.press(pp.PAST_CHATS_BUTTON)
    bob_entry = world.alice.session.last_buttons()[f"{BOB} · today"]
    await world.alice.press_data(bob_entry)
    await move_visits(session_factory, NOW - timedelta(days=2))
    await world.alice.press_data(bob_entry)
    assert len(notices_to(world.alice, BOB_ID)) == 2
    rows = await visit_rows(session_factory)
    assert len(rows) == 1 and rows[0][2] == NOW
    await open_visitors(world.bob)
    assert entries(world.bob) == [f"{ALICE} · today"]


async def test_visitors_are_listed_latest_first(
    world: World, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    carol = Chat(bot_settings, redis, session_factory, user_id=CAROL_ID)
    await complete_onboarding(carol)
    alice, bob, carol_id = (
        await world.user_id(USER_ID),
        await world.user_id(BOB_ID),
        await world.user_id(CAROL_ID),
    )
    async with session_factory() as session:
        await visits.record_visit(session, carol_id, bob, NOW - timedelta(days=3))
        await visits.record_visit(session, alice, bob, NOW - timedelta(days=1))
        await session.commit()
    await open_visitors(world.bob)
    assert entries(world.bob) == [f"{ALICE} · yesterday", f"{ALICE} · 3 days ago"]


async def test_no_notice_with_visit_alerts_off(world: World, session_factory: Sessions) -> None:
    bob = await world.user_id(BOB_ID)
    async with session_factory() as session:
        await users.set_notification(session, bob, Notification.VISIT_ALERTS, False)
        await session.commit()
    await alice_visits_bob(world)
    assert notices_to(world.alice, BOB_ID) == []
    await open_visitors(world.bob)
    assert entries(world.bob) == [f"{ALICE} · today"]


async def test_a_blocked_visitor_is_listed_and_marked_but_never_notifies(world: World) -> None:
    await open_people(world.bob)
    await world.bob.press(pp.PAST_CHATS_BUTTON)
    await world.bob.press(f"{ALICE} · today")
    await world.bob.press_data(own_buttons(world.bob)[BLOCK_BUTTON])
    await alice_visits_bob(world)
    assert notices_to(world.alice, BOB_ID) == []
    await open_visitors(world.bob)
    assert entries(world.bob) == [f"{ALICE} · today 🚫"]


async def test_the_partner_card_during_a_chat_is_listed_without_a_notice(
    world: World, session_factory: Sessions
) -> None:
    await match(world)
    await world.alice.send(PARTNER_PROFILE)
    assert notices_to(world.alice, BOB_ID) == []
    assert len(await visit_rows(session_factory)) == 1
    await world.alice.send(END_CHAT)
    await open_visitors(world.bob)
    assert entries(world.bob) == [f"{ALICE} · today"]


async def test_no_notice_while_chatting_together(world: World) -> None:
    await alice_visits_bob(world)
    data = world.alice.session.last_buttons()[pp.VIEW_PROFILE_BUTTON]
    await match(world)
    await world.bob.press_data(data)  # Bob opens Alice's card from the notice
    assert notices_to(world.bob, USER_ID) == []


async def test_a_failed_notice_still_records_the_visit(
    world: World, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    telegram = world.alice.session
    record = telegram.make_request

    async def refuse_bob(bot: Any, method: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage) and int(method.chat_id) == BOB_ID:
            raise TelegramForbiddenError(method=method, message="bot was blocked by the user")
        return await record(bot, method, timeout)

    monkeypatch.setattr(telegram, "make_request", refuse_bob)
    await alice_visits_bob(world)
    assert text_of(last_to(telegram, USER_ID)).startswith(pp.PAST_CHAT_HEADER)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    assert await visit_rows(session_factory) == [(alice, bob, NOW)]


async def test_visits_cost_nothing_and_never_record_self_visits(
    world: World, session_factory: Sessions
) -> None:
    alice = await world.user_id(USER_ID)
    async with session_factory() as session:
        assert not await visits.record_visit(session, alice, alice, NOW)
        await session.commit()
    assert await visit_rows(session_factory) == []


async def test_no_notice_when_the_visit_is_not_recorded(
    world: World, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def skipped(*args: object) -> bool:
        return False  # as when an account was deleted at that moment

    monkeypatch.setattr(visits, "record_visit", skipped)
    await alice_visits_bob(world)
    assert text_of(last_to(world.alice.session, USER_ID)).startswith(pp.PAST_CHAT_HEADER)
    assert notices_to(world.alice, BOB_ID) == []


async def test_a_late_write_never_moves_a_visit_back(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        await visits.record_visit(session, alice, bob, NOW)
        await visits.record_visit(session, alice, bob, NOW - timedelta(minutes=1))
        await session.commit()
    assert await visit_rows(session_factory) == [(alice, bob, NOW)]


# --- Who is listed ----------------------------------------------------------------------


async def test_nobody_listed_before_any_visit(world: World) -> None:
    assert await open_visitors(world.bob) == "Nobody visited your profile in the last 30 days."


async def test_old_visits_are_not_listed_or_opened(world: World, session_factory: Sessions) -> None:
    await alice_visits_bob(world)
    alice = await world.user_id(USER_ID)
    await move_visits(session_factory, NOW - timedelta(days=30, minutes=1))
    assert await open_visitors(world.bob) == "Nobody visited your profile in the last 30 days."
    await world.bob.press_data(visitor_data(alice))
    assert world.bob.session.callback_answers()[-1] == pp.NOT_AVAILABLE
    await set_setting(session_factory, SettingKey.VISITS_DAYS, 31)
    world.bob.restart()  # a fresh process, without the cached setting
    await open_visitors(world.bob)
    assert entries(world.bob) == [f"{ALICE} · 30 days ago"]


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_restricted_visitors_are_not_listed_or_opened(
    world: World, session_factory: Sessions, status: str
) -> None:
    await alice_visits_bob(world)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status=status)
        )
        await session.commit()
    assert await open_visitors(world.bob) == "Nobody visited your profile in the last 30 days."
    await world.bob.press_data(visitor_data(await world.user_id(USER_ID)))
    assert world.bob.session.callback_answers()[-1] == pp.NOT_AVAILABLE


async def test_someone_who_never_visited_cannot_be_opened(
    world: World, bot_settings: BotSettings, redis: Redis, session_factory: Sessions
) -> None:
    await alice_visits_bob(world)
    carol = Chat(bot_settings, redis, session_factory, user_id=CAROL_ID)
    await complete_onboarding(carol)
    await carol.press_data(visitor_data(await world.user_id(USER_ID)))
    assert carol.session.callback_answers()[-1] == pp.NOT_AVAILABLE


@pytest.mark.parametrize("who", [USER_ID, BOB_ID])
async def test_account_deletion_erases_visits_both_ways(
    world: World, session_factory: Sessions, who: int
) -> None:
    await alice_visits_bob(world)
    async with session_factory() as session:
        await visits.record_visit(
            session, await world.user_id(BOB_ID), await world.user_id(USER_ID), NOW
        )
        await users.delete_account(session, await world.user_id(who), NOW)
        await session.commit()
    assert await visit_rows(session_factory) == []


# --- Retention --------------------------------------------------------------------------


async def test_the_worker_deletes_expired_visits_once_a_day(
    world: World, session_factory: Sessions, redis: Redis
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        await visits.record_visit(session, alice, bob, NOW - timedelta(days=31))
        await visits.record_visit(session, bob, alice, NOW - timedelta(days=29))
        await session.commit()
    job = visits.VisitRetention(redis, session_factory, RuntimeSettings(ttl_seconds=0))
    assert await job.tick(NOW) == 1
    assert await visit_rows(session_factory) == [(bob, alice, NOW - timedelta(days=29))]
    assert await job.tick(NOW + timedelta(hours=1)) is None  # not due again yet


async def test_expired_visits_are_deleted_in_batches(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        await visits.record_visit(session, alice, bob, NOW - timedelta(days=40))
        await visits.record_visit(session, bob, alice, NOW - timedelta(days=40))
        await session.commit()
    async with session_factory() as session:
        assert await visits.delete_expired_visits(session, NOW, 1) == 1
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(ProfileVisit))
    assert count == 1


# --- Account deletion at the same time ---------------------------------------------------


async def test_a_visit_waiting_on_an_account_deletion_is_skipped(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as deleting:
        await users.delete_account(deleting, bob, NOW)

        async def visit() -> None:
            async with session_factory() as session:
                assert not await visits.record_visit(session, alice, bob, NOW)
                await session.commit()

        task = asyncio.create_task(visit())
        await wait_until_blocked(session_factory)
        assert not task.done()  # waits for the deletion's lock on Bob
        await deleting.commit()
        await task
    assert await visit_rows(session_factory) == []


async def test_block_and_report_ignore_chats_from_before_an_account_deletion(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        await users.delete_account(session, bob, NOW)
        await session.commit()
    await complete_onboarding(world.bob)
    async with session_factory() as session:
        assert await visits.latest_conversation(session, alice, bob) is None
        await visits.record_visit(session, bob, alice, NOW)
        await session.commit()
    await world.alice.press_data(visitor_data(bob))
    card = last_to(world.alice.session, USER_ID)
    assert text_of(card).startswith(pp.VISITOR_HEADER)
    # The old chat is ignored: Block and Report are for the profile (D-054).
    data = own_buttons(world.alice)
    assert list(data) == [BLOCK_BUTTON, REPORT_BUTTON]
    assert all(value.startswith("psafety:") for value in data.values())
