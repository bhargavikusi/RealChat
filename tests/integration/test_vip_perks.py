"""The VIP badge and matching priority (D-063)."""

from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import chat_requests as cr
from apps.bot import direct as dm
from apps.bot import likes as lk
from apps.bot import people as pp
from apps.bot.badges import CARD_BADGE, NOTICE_MARK
from apps.bot.endings import DirectAction, LikeAction, RequestAction
from apps.bot.keyboards import END_CHAT, PARTNER_PROFILE
from backend.config.settings import BotSettings
from backend.modules.matching.queue import VIP_KEY
from backend.modules.users import service as users
from backend.modules.users.models import User
from tests.support.chat import USER_ID
from tests.support.world import (
    BOB_ID,
    World,
    buttons,
    last_to,
    match,
    onboarded_world,
    own_buttons,
    search,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
ALICE = "Night Owl"
BOB = "Moon Child"


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    return await onboarded_world(bot_settings, redis, session_factory)


async def set_vip(sessions: Sessions, telegram_id: int, until: datetime | None) -> None:
    """VIP until `until`, with Ghost mode off so the VIP's visits are seen (D-064)."""
    async with sessions() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == telegram_id)
            .values(vip_until=until, ghost_mode=False)
        )
        await session.commit()


def soon() -> datetime:
    return datetime.now(UTC) + timedelta(days=30)


async def public_id(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await users.get_profile(session, user.id)).public_id


# --- Searching -------------------------------------------------------------------------


async def test_a_vip_search_is_recorded_for_the_matcher(world: World, redis: Redis) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    await search(world.alice)
    await search(world.bob)
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    assert await redis.zscore(VIP_KEY, str(alice)) is not None
    assert await redis.zscore(VIP_KEY, str(bob)) is None


# --- Cards -----------------------------------------------------------------------------


async def test_a_vip_partner_card_shows_the_badge(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    await match(world)
    # The match announcements: Bob sees Alice's badge; Alice sees Bob without one.
    assert f"👤 {ALICE} {CARD_BADGE}\n" in text_of(last_to(world.worker, BOB_ID))
    assert f"👤 {BOB}\n" in text_of(last_to(world.worker, USER_ID))
    assert f"👤 {ALICE} {CARD_BADGE}\n" in await world.bob.send(PARTNER_PROFILE)


async def test_the_badge_is_shown_only_while_vip_lasts(world: World) -> None:
    assert f"👤 {ALICE}\n" in await world.alice.send("/profile")
    await set_vip(world.sessions, USER_ID, soon())
    assert f"👤 {ALICE} {CARD_BADGE}\n" in await world.alice.send("/profile")
    await set_vip(world.sessions, USER_ID, datetime.now(UTC) - timedelta(seconds=1))
    assert f"👤 {ALICE}\n" in await world.alice.send("/profile")


async def test_userid_cards_show_the_badge(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    alice_id = await public_id(world.sessions, USER_ID)
    await world.bob.send(f"/userid {alice_id}")
    assert f"👤 {ALICE} {CARD_BADGE}\n" in text_of(last_to(world.bob.session, BOB_ID))
    # Her own card, too.
    assert f"👤 {ALICE} {CARD_BADGE}\n" in await world.alice.send(f"/userid {alice_id}")


# --- People lists ----------------------------------------------------------------------


async def test_people_lists_show_the_badge(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    await match(world)
    await world.alice.send(END_CHAT)
    await world.bob.send("/profile")
    await world.bob.press(pp.PEOPLE_BUTTON)
    await world.bob.press(pp.PAST_CHATS_BUTTON)
    [entry] = buttons(last_to(world.bob.session, BOB_ID))
    assert entry.startswith(f"{ALICE} {CARD_BADGE} · ")
    # Bob, not VIP, is listed plainly for Alice.
    await world.alice.send("/profile")
    await world.alice.press(pp.PEOPLE_BUTTON)
    await world.alice.press(pp.PAST_CHATS_BUTTON)
    [entry] = buttons(last_to(world.alice.session, USER_ID))
    assert entry.startswith(f"{BOB} · ")
    # Alice opened Bob's card from his /userid; she is in his Visited you.
    await world.alice.send(f"/userid {await public_id(world.sessions, BOB_ID)}")
    await world.bob.send("/profile")
    await world.bob.press(pp.PEOPLE_BUTTON)
    await world.bob.press(pp.VISITORS_BUTTON)
    [entry] = buttons(last_to(world.bob.session, BOB_ID))
    assert entry.startswith(f"{ALICE} {CARD_BADGE} · ")
    # And in his Liked you once she likes him.
    await world.alice.press_data(
        LikeAction(action="like", profile=await public_id(world.sessions, BOB_ID)).pack()
    )
    await world.bob.send("/profile")
    await world.bob.press(pp.PEOPLE_BUTTON)
    await world.bob.press(pp.LIKERS_BUTTON)
    [entry] = buttons(last_to(world.bob.session, BOB_ID))
    assert entry.startswith(f"{ALICE} {CARD_BADGE} · ")


# --- Notices ---------------------------------------------------------------------------


async def test_notices_mark_a_vip_with_the_tick(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    marked = f"{ALICE} {NOTICE_MARK}"
    bob_id = await public_id(world.sessions, BOB_ID)
    # A profile visit, a like, a direct message, and a chat request from Alice.
    await world.alice.send(f"/userid {bob_id}")
    assert pp.VISIT_NOTICE.format(nickname=marked) in [
        text_of(m) for m in world.alice.session.sent_to(BOB_ID)
    ]
    await world.alice.press_data(LikeAction(action="like", profile=bob_id).pack())
    assert text_of(last_to(world.alice.session, BOB_ID)) == lk.LIKE_NOTICE.format(nickname=marked)
    # Bob's notices about Alice name her with the tick; hers about Bob don't.
    await world.bob.send(f"/userid {await public_id(world.sessions, USER_ID)}")
    assert pp.VISIT_NOTICE.format(nickname=BOB) in [
        text_of(m) for m in world.bob.session.sent_to(USER_ID)
    ]


async def test_message_and_request_notices_mark_a_vip(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    marked = f"{ALICE} {NOTICE_MARK}"
    alice_id = await public_id(world.sessions, USER_ID)
    bob_id = await public_id(world.sessions, BOB_ID)
    # A direct message: written, previewed, and sent.
    await world.alice.press_data(DirectAction(action="write", profile=bob_id).pack())
    # Bob isn't VIP, so the prompt names him without the tick.
    assert text_of(last_to(world.alice.session, USER_ID)).startswith(
        dm.WRITE_PROMPT.split("{")[0] + f"{BOB}."
    )
    await world.alice.send("hello")
    await world.alice.press_data(own_buttons(world.alice)[dm.SEND_BUTTON])
    assert dm.MESSAGE_NOTICE.format(nickname=marked, public_id=alice_id) in [
        text_of(m) for m in world.alice.session.sent_to(BOB_ID)
    ]
    # A chat request.
    await world.alice.press_data(RequestAction(action="ask", profile=bob_id).pack())
    await world.alice.press_data(own_buttons(world.alice)[cr.SEND_BUTTON])
    assert any(
        text_of(m).startswith(f"💬 {marked} · /userid_{alice_id} wants to chat")
        for m in world.alice.session.sent_to(BOB_ID)
    )
    # Bob writing to Alice sees her with the tick.
    await world.bob.press_data(DirectAction(action="write", profile=alice_id).pack())
    assert text_of(last_to(world.bob.session, BOB_ID)).startswith(
        dm.WRITE_PROMPT.split("{")[0] + marked
    )


async def test_a_mark_does_not_outlast_vip_while_writing(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    alice_id = await public_id(world.sessions, USER_ID)
    await world.bob.press_data(DirectAction(action="write", profile=alice_id).pack())
    # Alice's VIP ends while Bob writes: the preview and his confirmation drop the tick.
    await set_vip(world.sessions, USER_ID, datetime.now(UTC) - timedelta(seconds=1))
    await world.bob.send("hello")
    assert dm.PREVIEW.format(nickname=ALICE) in [
        text_of(m) for m in world.bob.session.sent_to(BOB_ID)
    ]
    await world.bob.press_data(own_buttons(world.bob)[dm.SEND_BUTTON])
    assert text_of(last_to(world.bob.session, BOB_ID)).startswith(
        dm.DELIVERED.split("{")[0] + f"{ALICE}."
    )
