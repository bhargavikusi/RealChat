"""Profile likes (D-056): Like and Unlike on cards, notices, and the two lists."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import likes as lk
from apps.bot import people as pp
from apps.bot import safety as sf
from apps.bot.endings import (
    BLOCK_BUTTON,
    LIKE_BUTTON,
    REPORT_BUTTON,
    UNBLOCK_BUTTON,
    UNLIKE_BUTTON,
    LikeAction,
)
from apps.bot.keyboards import END_CHAT, PARTNER_PROFILE
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import SettingKey
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinTransaction
from backend.modules.people import likes
from backend.modules.people.likes import LikeOutcome
from backend.modules.people.models import ProfileLike
from backend.modules.people.service import PAGE_SIZE
from backend.modules.users import service as users
from backend.modules.users.models import User
from backend.modules.users.service import Notification
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked
from tests.support.world import (
    ALERT,
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
ALICE = "Night Owl"
BOB = "Moon Child"
GAP = timedelta(hours=1)
# Midday today: likes, labels, and the notice gap all read this one time, so a test never
# straddles midnight, and the date stays recent for checks that read the real clock.
NOW = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "_now", lambda: NOW)
    monkeypatch.setattr(lk, "_now", lambda: NOW)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    """Alice and Bob, onboarded, who have never chatted."""
    return await onboarded_world(bot_settings, redis, session_factory)


async def public_id(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await users.get_profile(session, user.id)).public_id


def card_markup(chat: Chat) -> InlineKeyboardMarkup:
    markup = last_to(chat.session, chat.user_id).reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return markup


def last_edit(chat: Chat) -> list[str]:
    edits = [m for m in chat.session.requests if isinstance(m, EditMessageReplyMarkup)]
    markup = edits[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.text for row in markup.inline_keyboard for b in row]


async def alice_opens_bob(world: World, sessions: Sessions) -> None:
    await world.alice.send(f"/userid_{await public_id(sessions, BOB_ID)}")


async def like_data(sessions: Sessions, telegram_id: int, action: str = "like") -> str:
    return LikeAction(action=action, profile=await public_id(sessions, telegram_id)).pack()


def like_notices(chat: Chat, telegram_id: int) -> list[str]:
    """Like notices `chat`'s bot sent to `telegram_id`."""
    return [text_of(m) for m in chat.session.sent_to(telegram_id) if "liked your" in text_of(m)]


def answer(chat: Chat) -> str:
    return chat.session.callback_answers()[-1]


async def like_rows(sessions: Sessions) -> list[tuple[int, int, bool]]:
    async with sessions() as session:
        rows = await session.execute(
            select(ProfileLike.liker_user_id, ProfileLike.liked_user_id, ProfileLike.liked_at)
        )
        return [(int(a), int(b), liked_at is not None) for a, b, liked_at in rows]


async def move_notice(sessions: Sessions, back: timedelta) -> None:
    async with sessions() as session:
        await session.execute(
            update(ProfileLike).values(notified_at=ProfileLike.notified_at - back)
        )
        await session.commit()


async def open_list(chat: Chat, button: str) -> str:
    await chat.send("/profile")
    await chat.press(pp.PEOPLE_BUTTON)
    return await chat.press(button)


def entries(chat: Chat) -> list[str]:
    return buttons(last_to(chat.session, chat.user_id))


async def set_status(sessions: Sessions, telegram_id: int, status: str) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == telegram_id).values(status=status)
        )
        await session.commit()


# --- Liking from a card ------------------------------------------------------------------


async def test_a_card_offers_like_and_liking_tells_the_liked_user(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    markup = card_markup(world.alice)
    assert buttons(last_to(world.alice.session, USER_ID)) == [
        LIKE_BUTTON,
        MESSAGE,
        REQUEST,
        ALERT,
        BLOCK_BUTTON,
        REPORT_BUTTON,
    ]
    await world.alice.press_on(markup, LIKE_BUTTON)
    assert answer(world.alice) == lk.LIKED
    # The same card now offers Unlike, keeping Block and Report.
    assert last_edit(world.alice) == [
        UNLIKE_BUTTON,
        MESSAGE,
        REQUEST,
        ALERT,
        BLOCK_BUTTON,
        REPORT_BUTTON,
    ]
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    assert await like_rows(session_factory) == [(alice, bob, True)]
    notice = last_to(world.alice.session, BOB_ID)
    assert text_of(notice) == f"❤️ {ALICE} liked your profile."
    assert buttons(notice) == [pp.VIEW_PROFILE_BUTTON]


async def test_repeated_taps_like_once_and_notify_once(
    world: World, session_factory: Sessions
) -> None:
    data = await like_data(session_factory, BOB_ID)
    await world.alice.press_data(data)
    await world.alice.press_data(data)
    assert answer(world.alice) == lk.ALREADY_LIKED
    assert len(await like_rows(session_factory)) == 1
    assert like_notices(world.alice, BOB_ID) == [f"❤️ {ALICE} liked your profile."]


async def test_concurrent_likes_add_one_like_and_claim_one_notice(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    now = NOW
    async with session_factory() as first, session_factory() as second:
        one = await likes.like(first, alice, bob, now, GAP)
        # The second like waits on the first's row until it commits.
        other = asyncio.create_task(likes.like(second, alice, bob, now, GAP))
        await wait_until_blocked(session_factory)
        await first.commit()
        two = await other
        await second.commit()
    assert (one.outcome, one.notify) == (LikeOutcome.LIKED, True)
    assert (two.outcome, two.notify) == (LikeOutcome.ALREADY, False)
    assert await like_rows(session_factory) == [(alice, bob, True)]


async def test_concurrent_relikes_claim_one_notice_after_the_gap(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    now = NOW
    async with session_factory() as session:
        await likes.like(session, alice, bob, now - 2 * GAP, GAP)
        await likes.unlike(session, alice, bob)
        await session.commit()
    async with session_factory() as first, session_factory() as second:
        one = await likes.like(first, alice, bob, now, GAP)
        other = asyncio.create_task(likes.like(second, alice, bob, now, GAP))
        await wait_until_blocked(session_factory)
        await first.commit()
        two = await other
        await second.commit()
    assert [one.notify, two.notify] == [True, False]


async def test_unliking_is_silent_and_the_card_offers_like_again(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    await world.alice.press_on(card_markup(world.alice), LIKE_BUTTON)
    await alice_opens_bob(world, session_factory)
    markup = card_markup(world.alice)
    assert buttons(last_to(world.alice.session, USER_ID))[0] == UNLIKE_BUTTON
    sent_to_bob = len(world.alice.session.sent_to(BOB_ID))
    await world.alice.press_on(markup, UNLIKE_BUTTON)
    assert answer(world.alice) == lk.UNLIKED
    assert last_edit(world.alice) == [
        LIKE_BUTTON,
        MESSAGE,
        REQUEST,
        ALERT,
        BLOCK_BUTTON,
        REPORT_BUTTON,
    ]
    assert len(world.alice.session.sent_to(BOB_ID)) == sent_to_bob
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    assert await like_rows(session_factory) == [(alice, bob, False)]
    await world.alice.press_data(await like_data(session_factory, BOB_ID, "unlike"))
    assert answer(world.alice) == lk.NOT_LIKED


async def test_liking_again_within_the_gap_sends_no_second_notice(
    world: World, session_factory: Sessions
) -> None:
    like, unlike = (
        await like_data(session_factory, BOB_ID),
        await like_data(session_factory, BOB_ID, "unlike"),
    )
    await world.alice.press_data(like)
    await world.alice.press_data(unlike)
    await world.alice.press_data(like)
    assert answer(world.alice) == lk.LIKED
    assert len(like_notices(world.alice, BOB_ID)) == 1
    # Once the gap has passed, a new like is announced again.
    await world.alice.press_data(unlike)
    await move_notice(session_factory, GAP)
    await world.alice.press_data(like)
    assert len(like_notices(world.alice, BOB_ID)) == 2


async def test_the_gap_is_an_admin_setting(world: World, session_factory: Sessions) -> None:
    async with session_factory() as session:
        session.add(AppSetting(key=SettingKey.LIKE_NOTICE_GAP_HOURS.value, value=3))
        await session.commit()
    like, unlike = (
        await like_data(session_factory, BOB_ID),
        await like_data(session_factory, BOB_ID, "unlike"),
    )
    await world.alice.press_data(like)
    await world.alice.press_data(unlike)
    await move_notice(session_factory, timedelta(hours=2))
    await world.alice.press_data(like)
    assert len(like_notices(world.alice, BOB_ID)) == 1
    await world.alice.press_data(unlike)
    await move_notice(session_factory, timedelta(hours=1))
    await world.alice.press_data(like)
    assert len(like_notices(world.alice, BOB_ID)) == 2


async def test_no_notice_with_like_alerts_off(world: World, session_factory: Sessions) -> None:
    bob = await world.user_id(BOB_ID)
    async with session_factory() as session:
        await users.set_notification(session, bob, Notification.LIKE_ALERTS, False)
        await session.commit()
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    assert answer(world.alice) == lk.LIKED
    assert like_notices(world.alice, BOB_ID) == []
    # A like nobody was told about doesn't hold back the next notice.
    async with session_factory() as session:
        await users.set_notification(session, bob, Notification.LIKE_ALERTS, True)
        await session.commit()
    await world.alice.press_data(await like_data(session_factory, BOB_ID, "unlike"))
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    assert like_notices(world.alice, BOB_ID) == [f"❤️ {ALICE} liked your profile."]


async def test_the_notice_button_opens_the_likers_card_as_a_visit(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    data = world.alice.session.last_buttons()[pp.VIEW_PROFILE_BUTTON]
    await world.bob.press_data(data)
    card = last_to(world.bob.session, BOB_ID)
    assert text_of(card).startswith(f"{pp.LIKER_HEADER}\n\n👤 {ALICE}")
    assert buttons(card) == [LIKE_BUTTON, MESSAGE, REQUEST, ALERT, BLOCK_BUTTON, REPORT_BUTTON]
    assert f"👀 {BOB} viewed your profile." in map(text_of, world.bob.session.sent_to(USER_ID))
    # Once Alice unlikes, the button opens nobody.
    await world.alice.press_data(await like_data(session_factory, BOB_ID, "unlike"))
    await world.bob.press_data(data)
    assert answer(world.bob) == pp.NOT_AVAILABLE


async def test_a_like_from_the_chat_partner_says_so(
    world: World, session_factory: Sessions
) -> None:
    await match(world)
    await world.alice.send(PARTNER_PROFILE)
    markup = card_markup(world.alice)
    assert buttons(last_to(world.alice.session, USER_ID)) == [
        LIKE_BUTTON,
        BLOCK_BUTTON,
        REPORT_BUTTON,
    ]
    await world.alice.press_on(markup, LIKE_BUTTON)
    notice = last_to(world.alice.session, BOB_ID)
    assert text_of(notice) == lk.PARTNER_LIKE_NOTICE
    assert buttons(notice) == []
    # After the chat, a like names the liker as usual.
    await world.alice.send(END_CHAT)
    await world.alice.press_data(await like_data(session_factory, BOB_ID, "unlike"))
    await move_notice(session_factory, GAP)
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    assert text_of(last_to(world.alice.session, BOB_ID)) == f"❤️ {ALICE} liked your profile."


# --- Who can like whom -------------------------------------------------------------------


async def test_you_cannot_like_someone_you_blocked(world: World, session_factory: Sessions) -> None:
    await alice_opens_bob(world, session_factory)
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    assert answer(world.alice) == lk.UNBLOCK_TO_LIKE
    assert await like_rows(session_factory) == []
    assert like_notices(world.alice, BOB_ID) == []


async def test_a_blocked_users_like_still_notifies_and_is_marked(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    await world.bob.press_data(world.alice.session.last_buttons()[pp.VIEW_PROFILE_BUTTON])
    await world.bob.press_data(own_buttons(world.bob)[BLOCK_BUTTON])
    await world.alice.press_data(await like_data(session_factory, BOB_ID, "unlike"))
    await move_notice(session_factory, GAP)
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    assert answer(world.alice) == lk.LIKED
    assert len(like_notices(world.alice, BOB_ID)) == 2
    assert await open_list(world.bob, pp.LIKERS_BUTTON) == pp.LIKERS_HEADER
    assert entries(world.bob) == [f"{ALICE} · today 🚫"]


async def test_blocking_from_a_card_keeps_its_like_button(
    world: World, session_factory: Sessions
) -> None:
    await alice_opens_bob(world, session_factory)
    await world.alice.press_on(card_markup(world.alice), BLOCK_BUTTON)
    assert answer(world.alice) == sf.BLOCKED
    assert last_edit(world.alice) == [
        LIKE_BUTTON,
        MESSAGE,
        REQUEST,
        ALERT,
        UNBLOCK_BUTTON,
        REPORT_BUTTON,
    ]


async def test_a_forged_button_cannot_like_yourself(
    world: World, session_factory: Sessions
) -> None:
    await world.alice.press_data(await like_data(session_factory, USER_ID))
    assert answer(world.alice) == pp.NOT_AVAILABLE
    assert await like_rows(session_factory) == []


@pytest.mark.parametrize("status", ["suspended", "banned"])
async def test_an_unavailable_account_cannot_be_liked(
    world: World, session_factory: Sessions, status: str
) -> None:
    data = await like_data(session_factory, BOB_ID)
    await set_status(session_factory, BOB_ID, status)
    await world.alice.press_data(data)
    assert answer(world.alice) == pp.NOT_AVAILABLE
    assert await like_rows(session_factory) == []


async def test_an_unknown_id_likes_nobody(world: World) -> None:
    await world.alice.press_data(LikeAction(action="like", profile="zzzzzzz").pack())
    assert answer(world.alice) == pp.NOT_AVAILABLE


async def test_an_unfinished_profile_cannot_be_liked(
    world: World, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    carol = Chat(bot_settings, redis, session_factory, user_id=CAROL_ID)
    await carol.send("/start")
    await world.alice.press_data(await like_data(session_factory, CAROL_ID))
    assert answer(world.alice) == pp.NOT_AVAILABLE
    assert await like_rows(session_factory) == []


async def test_liking_charges_no_coins(world: World, session_factory: Sessions) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)

    async def ledger() -> tuple[int, int, int]:
        async with session_factory() as session:
            entries = await session.scalar(select(func.count()).select_from(CoinTransaction))
            return (
                int(entries or 0),
                (await coins.balance(session, alice)).total,
                (await coins.balance(session, bob)).total,
            )

    before = await ledger()
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    await world.alice.press_data(await like_data(session_factory, BOB_ID, "unlike"))
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    await world.bob.press_data(world.alice.session.last_buttons()[pp.VIEW_PROFILE_BUTTON])
    assert await ledger() == before


# --- The lists ---------------------------------------------------------------------------


async def test_both_lists_show_current_likes_and_open_cards(
    world: World, session_factory: Sessions
) -> None:
    assert await open_list(world.bob, pp.LIKERS_BUTTON) == pp.NO_LIKERS
    assert await open_list(world.alice, pp.LIKING_BUTTON) == pp.NO_LIKING
    await world.alice.press_data(await like_data(session_factory, BOB_ID))
    assert await open_list(world.bob, pp.LIKERS_BUTTON) == pp.LIKERS_HEADER
    assert entries(world.bob) == [f"{ALICE} · today"]
    assert await open_list(world.alice, pp.LIKING_BUTTON) == pp.LIKING_HEADER
    assert entries(world.alice) == [f"{BOB} · today"]
    await world.alice.press(f"{BOB} · today")
    card = last_to(world.alice.session, USER_ID)
    assert text_of(card).startswith(f"{pp.LIKED_HEADER}\n\n👤 {BOB}")
    assert buttons(card) == [UNLIKE_BUTTON, MESSAGE, REQUEST, ALERT, BLOCK_BUTTON, REPORT_BUTTON]
    # Unliked: gone from both lists, and the old entry opens nobody.
    await world.alice.press_data(await like_data(session_factory, BOB_ID, "unlike"))
    assert await open_list(world.bob, pp.LIKERS_BUTTON) == pp.NO_LIKERS
    await world.alice.press_data(
        pp.PeopleAction(action="liked", user=await world.user_id(BOB_ID)).pack()
    )
    assert answer(world.alice) == pp.NOT_AVAILABLE


async def test_lists_are_latest_first_and_skip_unavailable_accounts(
    world: World, session_factory: Sessions, bot_settings: BotSettings, redis: Redis
) -> None:
    carol = Chat(bot_settings, redis, session_factory, user_id=CAROL_ID)
    await complete_onboarding(carol)
    alice, bob, carol_id = (
        await world.user_id(USER_ID),
        await world.user_id(BOB_ID),
        await world.user_id(CAROL_ID),
    )
    now = NOW
    async with session_factory() as session:
        await likes.like(session, carol_id, bob, now - timedelta(days=3), GAP)
        await likes.like(session, alice, bob, now - timedelta(days=1), GAP)
        await session.commit()
    await open_list(world.bob, pp.LIKERS_BUTTON)
    assert entries(world.bob) == [f"{ALICE} · yesterday", f"{ALICE} · 3 days ago"]
    await set_status(session_factory, CAROL_ID, "suspended")
    await open_list(world.bob, pp.LIKERS_BUTTON)
    assert entries(world.bob) == [f"{ALICE} · yesterday"]
    await set_status(session_factory, BOB_ID, "banned")
    await open_list(world.alice, pp.LIKING_BUTTON)
    assert text_of(last_to(world.alice.session, USER_ID)) == pp.NO_LIKING


async def test_lists_come_in_pages(world: World, session_factory: Sessions) -> None:
    bob = await world.user_id(BOB_ID)
    now = NOW
    async with session_factory() as session:
        for n in range(PAGE_SIZE + 1):
            user = await users.get_or_create_user(session, 9000 + n)
            profile = await users.get_profile(session, user.id)
            profile.nickname = f"Liker {n:02}"
            profile.age_at_entry, profile.age_entered_on = 30, now.date()
            profile.gender, profile.country_code = "female", "IN"
            profile.onboarding_completed_at = now
            await session.flush()
            await likes.like(session, user.id, bob, now - timedelta(minutes=n), GAP)
        await session.commit()
    await open_list(world.bob, pp.LIKERS_BUTTON)
    first = entries(world.bob)
    assert first[0] == "Liker 00 · today" and first[-1] == pp.NEXT_BUTTON
    assert len(first) == PAGE_SIZE + 1
    await world.bob.press(pp.NEXT_BUTTON)
    assert entries(world.bob) == [f"Liker {PAGE_SIZE:02} · today", pp.PREVIOUS_BUTTON]


async def test_deleting_an_account_erases_its_likes_both_ways(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    now = NOW
    async with session_factory() as session:
        await likes.like(session, alice, bob, now, GAP)
        await likes.like(session, bob, alice, now, GAP)
        await session.commit()
    async with session_factory() as session:
        await users.delete_account(session, bob, now)
        await session.commit()
    assert await like_rows(session_factory) == []
    assert await open_list(world.alice, pp.LIKERS_BUTTON) == pp.NO_LIKERS


async def test_a_likers_card_waits_for_an_account_deletion_in_progress(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with session_factory() as session:
        await likes.like(session, alice, bob, NOW, GAP)
        await session.commit()
    async with session_factory() as deleting, session_factory() as reading:
        await users.delete_account(deleting, alice, NOW)
        pending = asyncio.create_task(likes.liker_profile(reading, bob, alice))
        await wait_until_blocked(session_factory)
        await deleting.commit()
        found = await pending
        await reading.commit()
    assert found is None


async def test_a_like_waits_for_an_account_deletion_in_progress(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    now = NOW
    async with session_factory() as deleting, session_factory() as liking:
        await users.delete_account(deleting, bob, now)
        pending = asyncio.create_task(likes.like(liking, alice, bob, now, GAP))
        await wait_until_blocked(session_factory)
        await deleting.commit()
        liked = await pending
        await liking.commit()
    assert liked.outcome == LikeOutcome.UNAVAILABLE
    assert await like_rows(session_factory) == []
