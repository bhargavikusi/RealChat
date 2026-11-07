"""RealChat IDs and /userid (D-054): every card shows an ID that opens it, as a visit,
and people met that way can block and report each other without a chat."""

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import onboarding as ob
from apps.bot import people as pp
from apps.bot import safety as sf
from apps.bot.endings import (
    BLOCK_BUTTON,
    LIKE_BUTTON,
    REPORT_BUTTON,
    UNBLOCK_BUTTON,
    ProfileSafetyAction,
)
from apps.bot.keyboards import END_CHAT
from apps.bot.menu import HELP_TEXT
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.moderation import service as moderation
from backend.modules.moderation.models import Report, UserBlock
from backend.modules.people import visits
from backend.modules.people.lookups import KEY_PREFIX, IdLookups, LookupsUnavailable
from backend.modules.people.models import ProfileVisit
from backend.modules.users import public_ids
from backend.modules.users import service as users
from backend.modules.users.models import Profile, User
from tests.support.chat import TERMS, USER_ID, Chat, complete_onboarding
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
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
ALICE = "Night Owl"
BOB = "Moon Child"


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    """Alice and Bob, onboarded, who have never chatted."""
    return await onboarded_world(bot_settings, redis, session_factory)


async def public_id(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await users.get_profile(session, user.id)).public_id


async def visit_pairs(sessions: Sessions) -> set[tuple[int, int]]:
    async with sessions() as session:
        rows = await session.execute(
            select(ProfileVisit.visitor_user_id, ProfileVisit.visited_user_id)
        )
        return {(int(a), int(b)) for a, b in rows}


async def set_setting(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        session.add(AppSetting(key=key.value, value=value))
        await session.commit()


async def alice_opens_bob(world: World, sessions: Sessions) -> str:
    """Alice opens Bob's card by his ID; returns his ID."""
    bob_id = await public_id(sessions, BOB_ID)
    await world.alice.send(f"/userid_{bob_id}")
    return bob_id


def own_text(chat: Chat) -> str:
    """The latest message to the user's own chat, ignoring notices sent to others."""
    return text_of(last_to(chat.session, chat.user_id))


async def press_own(chat: Chat, button: str) -> None:
    await chat.press_data(own_buttons(chat)[button])


def safety_data(chat: Chat) -> list[str]:
    """The callback data of Block and Report on the latest card, without Like."""
    data = own_buttons(chat)
    return [data[BLOCK_BUTTON if BLOCK_BUTTON in data else UNBLOCK_BUTTON], data[REPORT_BUTTON]]


def notices_to(chat: Chat, telegram_id: int) -> list[str]:
    return [
        text_of(m) for m in chat.session.sent_to(telegram_id) if "viewed your profile" in text_of(m)
    ]


# --- The ID itself -------------------------------------------------------------------------


async def test_every_account_gets_its_own_random_id(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await public_id(session_factory, USER_ID), await public_id(session_factory, BOB_ID)
    assert alice != bob
    for value in (alice, bob):
        assert public_ids.normalize(value) == value
        assert len(value) == public_ids.LENGTH
    # Asking again never changes it.
    assert await public_id(session_factory, USER_ID) == alice


async def test_the_id_is_shown_on_every_card(world: World, session_factory: Sessions) -> None:
    alice = await public_id(session_factory, USER_ID)
    await world.alice.send("/profile")
    assert f"📍 India\n🆔 /userid_{alice}" in world.alice.last()
    # And on the card a chat partner sees.
    await match(world)
    assert any(
        f"👤 {ALICE}" in text and f"🆔 /userid_{alice}" in text
        for text in map(text_of, world.worker.sent_to(BOB_ID))
    )


async def test_a_drawn_id_that_is_taken_is_drawn_again(
    world: World, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = await public_id(session_factory, USER_ID)
    draws = iter([taken, "zzzzzzz"])
    monkeypatch.setattr(public_ids, "new_public_id", lambda: next(draws))
    async with session_factory() as session:
        user = await users.get_or_create_user(session, 7777)
        assert (await users.get_profile(session, user.id)).public_id == "zzzzzzz"


async def test_deleting_an_account_retires_its_id(world: World, session_factory: Sessions) -> None:
    old = await public_id(session_factory, BOB_ID)
    async with session_factory() as session:
        bob = await users.get_or_create_user(session, BOB_ID)
        await users.delete_account(session, bob.id, datetime.now(UTC))
        await session.commit()
    new = await public_id(session_factory, BOB_ID)
    assert new != old
    async with session_factory() as session:
        assert await users.user_id_for(session, old) is None
    await complete_onboarding(world.bob)
    assert await world.alice.send(f"/userid_{old}") == pp.NO_PROFILE
    await world.alice.send(f"/userid_{new}")
    assert own_text(world.alice).startswith(pp.ID_HEADER)


def test_ids_ignore_case_and_spaces_but_nothing_else() -> None:
    assert public_ids.normalize(" K7M3QXA ") == "k7m3qxa"
    # The Kelvin sign lowercases to "k" but is not an ASCII letter.
    for wrong in ("k7m3qx", "k7m3qxaa", "k7m3qx0", "k7m3qxl", "k7m3qx_", "", "\u212a7m3qxa"):
        assert public_ids.normalize(wrong) is None


async def test_text_after_the_id_makes_it_wrong(world: World, session_factory: Sessions) -> None:
    bob_id = await public_id(session_factory, BOB_ID)
    for text in (f"/userid_{bob_id} hello", f"/userid_{bob_id}-x", f"/userid {bob_id} x"):
        assert await world.alice.send(text) == pp.NO_PROFILE
    assert await visit_pairs(session_factory) == set()


# --- /userid ---------------------------------------------------------------------------------


async def test_userid_alone_shows_your_own_id(world: World, session_factory: Sessions) -> None:
    alice = await public_id(session_factory, USER_ID)
    assert await world.alice.send("/userid") == pp.OWN_ID.format(id=alice)
    assert "/userid - " in HELP_TEXT


@pytest.mark.parametrize("form", ["/userid_{id}", "/userid {id}", "/USERID_{upper}"])
async def test_both_forms_open_the_card_as_a_visit(
    world: World, session_factory: Sessions, form: str
) -> None:
    bob_id = await public_id(session_factory, BOB_ID)
    await world.alice.send(form.format(id=bob_id, upper=bob_id.upper()))
    card = last_to(world.alice.session, USER_ID)
    assert text_of(card).startswith(f"{pp.ID_HEADER}\n\n👤 {BOB}")
    assert buttons(card) == [LIKE_BUTTON, BLOCK_BUTTON, REPORT_BUTTON]
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    assert await visit_pairs(session_factory) == {(alice, bob)}
    assert notices_to(world.alice, BOB_ID) == [f"👀 {ALICE} viewed your profile."]


async def test_your_own_id_opens_your_card_without_a_visit(
    world: World, session_factory: Sessions
) -> None:
    alice = await public_id(session_factory, USER_ID)
    await world.alice.send(f"/userid_{alice}")
    card = last_to(world.alice.session, USER_ID)
    assert text_of(card).startswith(f"{pp.OWN_CARD_HEADER}\n\n👤 {ALICE}")
    assert buttons(card) == []
    assert await visit_pairs(session_factory) == set()


async def test_the_visited_user_can_open_the_visitor_and_act_on_them(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    data = world.alice.session.last_buttons()[pp.VIEW_PROFILE_BUTTON]
    await world.bob.press_data(data)
    assert text_of(last_to(world.bob.session, BOB_ID)).startswith(pp.VISITOR_HEADER)
    block = own_buttons(world.bob)[BLOCK_BUTTON]
    assert block.startswith("psafety:")
    await world.bob.press_data(block)
    assert world.bob.session.callback_answers()[-1] == sf.BLOCKED


async def test_a_past_partner_opened_by_id_gets_the_chats_buttons(
    world: World, session_factory: Sessions
) -> None:
    await match(world)
    # During the chat: the card acts on this chat, and the partner is not told.
    await alice_opens_bob(world, session_factory)
    assert safety_data(world.alice) and all(
        v.startswith("safety:") for v in safety_data(world.alice)
    )
    assert notices_to(world.alice, BOB_ID) == []
    await world.alice.send(END_CHAT)
    await alice_opens_bob(world, session_factory)
    assert safety_data(world.alice) and all(
        v.startswith("safety:") for v in safety_data(world.alice)
    )
    assert notices_to(world.alice, BOB_ID) == [f"👀 {ALICE} viewed your profile."]


@pytest.mark.parametrize("status", ["deleted", "suspended", "banned"])
async def test_unavailable_accounts_answer_like_unknown_ids(
    world: World, session_factory: Sessions, status: str
) -> None:
    bob_id = await public_id(session_factory, BOB_ID)
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == BOB_ID)
            .values(status=status, deleted_at=datetime.now(UTC))
        )
        await session.commit()
    assert await world.alice.send(f"/userid_{bob_id}") == pp.NO_PROFILE
    assert await world.alice.send("/userid zzzzzzz") == pp.NO_PROFILE
    assert await world.alice.send("/userid not-an-id") == pp.NO_PROFILE
    assert await visit_pairs(session_factory) == set()


async def test_an_unfinished_profile_answers_like_an_unknown_id(
    world: World, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    carol = Chat(bot_settings, redis, session_factory, user_id=6464)
    assert await carol.send("/start") == TERMS
    carol_id = await public_id(session_factory, 6464)
    assert await world.alice.send(f"/userid_{carol_id}") == pp.NO_PROFILE


async def test_lookups_need_a_finished_onboarding(
    world: World, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    carol = Chat(bot_settings, redis, session_factory, user_id=6464)
    bob_id = await public_id(session_factory, BOB_ID)
    assert await carol.send(f"/userid_{bob_id}") == TERMS
    await carol.press("I agree")
    assert await carol.send("/userid") == ob.PROMPTS["nickname"]
    assert await visit_pairs(session_factory) == set()


async def test_userid_is_ignored_in_groups(world: World, session_factory: Sessions) -> None:
    from tests.support.telegram import message_update

    bob_id = await public_id(session_factory, BOB_ID)
    before = len(world.alice.session.requests)
    await world.alice.dispatcher.feed_update(
        world.alice.bot, message_update(9001, USER_ID, f"/userid_{bob_id}", chat_type="group")
    )
    sent = world.alice.session.requests[before:]
    assert not any(bob_id in str(m) for m in sent)
    assert await visit_pairs(session_factory) == set()


# --- Wrong lookups ---------------------------------------------------------------------------


async def test_wrong_lookups_pause_all_lookups_for_the_hour(
    world: World, session_factory: Sessions, redis: Redis
) -> None:
    await set_setting(session_factory, SettingKey.USERID_MISSES_PER_HOUR, 2)
    world.alice.restart()  # without the cached settings
    bob_id = await public_id(session_factory, BOB_ID)
    assert await world.alice.send("/userid zzzzzzz") == pp.NO_PROFILE
    assert await world.alice.send("/userid yyyyyyy") == pp.NO_PROFILE
    # Even a correct ID is refused now, and refusals don't extend the pause.
    assert await world.alice.send(f"/userid_{bob_id}") == pp.LOOKUPS_PAUSED
    alice = await world.user_id(USER_ID)
    assert await redis.get(f"{KEY_PREFIX}{alice}") == "2"
    assert 0 < await redis.ttl(f"{KEY_PREFIX}{alice}") <= 3600
    # Your own ID still works, and other users are not affected.
    assert (await world.alice.send("/userid")).startswith("🆔 Your RealChat ID")
    alice_id = await public_id(session_factory, USER_ID)
    await world.bob.send(f"/userid_{alice_id}")
    assert own_text(world.bob).startswith(pp.ID_HEADER)
    # Once the hour is over, lookups work again.
    await redis.delete(f"{KEY_PREFIX}{alice}")
    await world.alice.send(f"/userid_{bob_id}")
    assert own_text(world.alice).startswith(pp.ID_HEADER)


async def test_correct_lookups_are_not_counted(
    world: World, session_factory: Sessions, redis: Redis
) -> None:
    bob_id = await public_id(session_factory, BOB_ID)
    for _ in range(3):
        await world.alice.send(f"/userid_{bob_id}")
    alice = await world.user_id(USER_ID)
    assert await redis.get(f"{KEY_PREFIX}{alice}") is None


async def test_lookups_are_refused_when_the_limit_cannot_be_checked(
    world: World, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(self: IdLookups, user_id: int, limit: int) -> bool:
        raise LookupsUnavailable

    monkeypatch.setattr(IdLookups, "paused", unavailable)
    bob_id = await public_id(session_factory, BOB_ID)
    assert await world.alice.send(f"/userid_{bob_id}") == pp.LOOKUPS_UNAVAILABLE
    assert await visit_pairs(session_factory) == set()


async def test_a_miss_is_refused_when_it_cannot_be_counted(
    world: World, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(self: IdLookups, user_id: int) -> None:
        raise LookupsUnavailable

    monkeypatch.setattr(IdLookups, "miss", unavailable)
    assert await world.alice.send("/userid zzzzzzz") == pp.LOOKUPS_UNAVAILABLE


# --- Block and Report without a chat ------------------------------------------------------------


async def block_rows(sessions: Sessions) -> list[tuple[int, int, int | None, bool]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                UserBlock.blocker_user_id,
                UserBlock.blocked_user_id,
                UserBlock.conversation_id,
                UserBlock.unblocked_at.is_(None),
            ).order_by(UserBlock.id)
        )
        return [(int(a), int(b), c, bool(d)) for a, b, c, d in rows]


async def report_rows(sessions: Sessions) -> list[tuple[int, int, int | None, str]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                Report.reporter_user_id,
                Report.reported_user_id,
                Report.conversation_id,
                Report.reason,
            ).order_by(Report.id)
        )
        return [(int(a), int(b), c, r) for a, b, c, r in rows]


def last_markup_buttons(chat: Chat) -> list[str]:
    edits = [m for m in chat.session.requests if isinstance(m, EditMessageReplyMarkup)]
    markup = edits[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_block_and_unblock_someone_met_by_id(world: World, session_factory: Sessions) -> None:
    await alice_opens_bob(world, session_factory)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    await press_own(world.alice, BLOCK_BUTTON)
    assert world.alice.session.callback_answers()[-1] == sf.BLOCKED
    assert last_markup_buttons(world.alice) == [UNBLOCK_BUTTON, REPORT_BUTTON]
    assert await block_rows(session_factory) == [(alice, bob, None, True)]
    await press_own(world.alice, BLOCK_BUTTON)
    assert world.alice.session.callback_answers()[-1] == sf.ALREADY_BLOCKED
    await world.alice.press_data(
        ProfileSafetyAction(
            action="unblock", profile=await public_id(session_factory, BOB_ID)
        ).pack()
    )
    assert world.alice.session.callback_answers()[-1] == sf.UNBLOCKED
    assert await block_rows(session_factory) == [(alice, bob, None, False)]
    # The card shows the block when opened again.
    await press_own(world.alice, BLOCK_BUTTON)
    await alice_opens_bob(world, session_factory)
    assert buttons(last_to(world.alice.session, USER_ID)) == [
        LIKE_BUTTON,
        UNBLOCK_BUTTON,
        REPORT_BUTTON,
    ]


async def test_report_someone_met_by_id_once(world: World, session_factory: Sessions) -> None:
    await alice_opens_bob(world, session_factory)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    report = own_buttons(world.alice)[REPORT_BUTTON]
    await press_own(world.alice, REPORT_BUTTON)
    assert world.alice.last() == sf.REPORT_PROMPT
    assert await world.alice.press(sf.REASON_LABELS["spam"]) == sf.DETAILS_PROMPT
    assert sf.REPORT_SENT in world.alice.session.sent_texts()
    assert await world.alice.send("Sends links to a scam site") == sf.DETAILS_ADDED
    assert await report_rows(session_factory) == [(alice, bob, None, "spam")]
    assert await block_rows(session_factory) == [(alice, bob, None, True)]
    async with session_factory() as session:
        details = await session.scalar(select(Report.details))
    assert details == "Sends links to a scam site"
    # Once per person: the button and an old reason button both refuse.
    await world.alice.press_data(report)
    assert world.alice.session.callback_answers()[-1] == sf.ALREADY_REPORTED_PROFILE
    public = await public_id(session_factory, BOB_ID)
    await world.alice.press_data(
        ProfileSafetyAction(action="reason", profile=public, reason="other").pack()
    )
    assert world.alice.session.callback_answers()[-1] == sf.ALREADY_REPORTED_PROFILE
    assert len(await report_rows(session_factory)) == 1


async def test_profile_reports_count_toward_the_daily_limit(
    world: World, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    await set_setting(session_factory, SettingKey.REPORTS_PER_DAY, 1)
    world.alice.restart()
    await match(world)
    await world.alice.send(END_CHAT)
    # A chat report uses up the day's one report.
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        conversation = await visits.latest_conversation(session, alice, bob)
        assert conversation is not None
        await moderation.file_report(
            session, alice, conversation, "spam", datetime.now(UTC), per_day=1
        )
        await session.commit()
    carol = Chat(bot_settings, redis, session_factory, user_id=6464)
    await complete_onboarding(carol)
    carol_id = await public_id(session_factory, 6464)
    await world.alice.send(f"/userid_{carol_id}")
    await press_own(world.alice, REPORT_BUTTON)
    await world.alice.press(sf.REASON_LABELS["other"])
    assert world.alice.session.callback_answers()[-1] == sf.LIMIT_REACHED
    assert len(await report_rows(session_factory)) == 1


async def test_profile_actions_need_a_visit_either_way(
    world: World, session_factory: Sessions
) -> None:
    bob = await public_id(session_factory, BOB_ID)
    for action in ("block", "report"):
        await world.alice.press_data(ProfileSafetyAction(action=action, profile=bob).pack())
        assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    await world.alice.press_data(
        ProfileSafetyAction(action="reason", profile=bob, reason="spam").pack()
    )
    assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    # Your own ID, even with a past chat that a "chat with yourself" lookup would find.
    await match(world)
    await world.alice.send(END_CHAT)
    alice = await public_id(session_factory, USER_ID)
    for action in ("block", "report"):
        await world.alice.press_data(ProfileSafetyAction(action=action, profile=alice).pack())
        assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    await world.alice.press_data(
        ProfileSafetyAction(action="reason", profile=alice, reason="spam").pack()
    )
    assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    # An ID nobody has, or a made-up reason.
    await world.alice.press_data(ProfileSafetyAction(action="block", profile="zzzzzzz").pack())
    assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    await world.alice.press_data(
        ProfileSafetyAction(action="reason", profile=bob, reason="bogus").pack()
    )
    assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    assert await block_rows(session_factory) == []
    assert await report_rows(session_factory) == []


async def test_a_retired_id_on_an_old_button_acts_on_nobody(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    block = own_buttons(world.alice)[BLOCK_BUTTON]
    async with session_factory() as session:
        bob = await users.get_or_create_user(session, BOB_ID)
        await users.delete_account(session, bob.id, datetime.now(UTC))
        await session.commit()
    await world.alice.press_data(block)
    assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    assert await block_rows(session_factory) == []


async def test_simultaneous_profile_reports_file_one(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)

    async def report() -> moderation.ReportOutcome:
        async with session_factory() as session:
            filed = await moderation.file_profile_report(
                session, alice, bob, "spam", datetime.now(UTC), per_day=10, since=LONG_AGO
            )
            await session.commit()
            return filed.outcome

    async with session_factory() as holding:
        # Hold Alice's row the way a report does, so both reports queue behind it.
        await holding.execute(
            select(User.id).where(User.id == alice).with_for_update(key_share=True)
        )
        tasks = [asyncio.create_task(report()) for _ in range(2)]
        await wait_until_blocked(session_factory)
        await holding.commit()
        outcomes = await asyncio.gather(*tasks)
    assert sorted(outcomes) == sorted(
        [moderation.ReportOutcome.FILED, moderation.ReportOutcome.ALREADY_REPORTED]
    )
    assert len(await report_rows(session_factory)) == 1


async def test_the_database_allows_one_profile_report_per_pair(
    world: World, session_factory: Sessions
) -> None:
    from sqlalchemy.exc import IntegrityError

    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        for _ in range(2):
            session.add(
                Report(
                    reporter_user_id=alice,
                    reported_user_id=bob,
                    conversation_id=None,
                    reason="spam",
                    urgent=False,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_profile_ids_are_unique_in_the_database(
    world: World, session_factory: Sessions
) -> None:
    from sqlalchemy.exc import IntegrityError

    alice = await public_id(session_factory, USER_ID)
    async with session_factory() as session:
        bob = await world.user_id(BOB_ID)
        with pytest.raises(IntegrityError):
            await session.execute(
                update(Profile).where(Profile.user_id == bob).values(public_id=alice)
            )


async def test_a_profile_button_acts_on_a_chat_started_after_the_card(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    block, report = own_buttons(world.alice)[BLOCK_BUTTON], own_buttons(world.alice)[REPORT_BUTTON]
    await match(world)
    # The old profile button now asks first, because blocking ends this chat.
    await world.alice.press_data(block)
    assert world.alice.last() == sf.CONFIRM_BLOCK
    # And Report files a report about the chat, which ends it.
    await world.alice.press_data(report)
    assert world.alice.last() == sf.REPORT_PROMPT
    assert all(v.startswith("safety:") for v in world.alice.session.last_buttons().values())
    await world.alice.press(sf.REASON_LABELS["spam"])
    assert sf.REPORT_SENT_ENDED in world.alice.session.sent_texts()
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    rows = await report_rows(session_factory)
    assert len(rows) == 1 and rows[0][:2] == (alice, bob) and rows[0][2] is not None


async def test_a_visit_outside_the_window_allows_no_profile_action(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    block = own_buttons(world.alice)[BLOCK_BUTTON]
    async with session_factory() as session:
        # Older than the 30-day window, but not yet removed by the daily cleanup.
        await session.execute(update(ProfileVisit).values(last_visited_at=LONG_AGO))
        await session.commit()
    await world.alice.press_data(block)
    assert world.alice.session.callback_answers()[-1] == sf.NOT_AVAILABLE
    assert await block_rows(session_factory) == []


async def test_a_profile_block_waits_for_a_deletion_and_then_fails(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as deleting:
        await users.delete_account(deleting, bob, datetime.now(UTC))

        async def block() -> None:
            async with session_factory() as session:
                with pytest.raises(moderation.NotPartnerError):
                    await moderation.block_profile(session, alice, bob, LONG_AGO)
                await session.rollback()

        task = asyncio.create_task(block())
        await wait_until_blocked(session_factory)
        assert not task.done()  # waits for the deletion's lock on Bob
        await deleting.commit()
        await task
    assert await block_rows(session_factory) == []


async def test_a_lookup_waits_for_a_deletion_and_then_finds_nobody(
    world: World, session_factory: Sessions
) -> None:
    bob = await world.user_id(BOB_ID)
    bob_id = await public_id(session_factory, BOB_ID)
    async with session_factory() as deleting:
        await users.delete_account(deleting, bob, datetime.now(UTC))

        async def lookup() -> Profile | None:
            async with session_factory() as session:
                return await users.find_profile(session, bob_id)

        task = asyncio.create_task(lookup())
        await wait_until_blocked(session_factory)
        assert not task.done()
        await deleting.commit()
        assert await task is None


async def test_the_database_refuses_a_malformed_id(world: World, session_factory: Sessions) -> None:
    from sqlalchemy.exc import IntegrityError

    bob = await world.user_id(BOB_ID)
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(Profile).where(Profile.user_id == bob).values(public_id="ABC0001")
            )
