"""Ghost mode, a VIP setting for profile visits (D-064)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import EditMessageReplyMarkup
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import people as pp
from apps.bot import vip as bot_vip
from apps.bot.endings import LikeAction
from apps.bot.keyboards import END_CHAT, PARTNER_PROFILE
from apps.bot.people import PeopleAction
from backend.config.settings import BotSettings
from backend.modules.payments.service import VipPass
from backend.modules.people.models import ProfileVisit
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
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
GHOST_ON = f"{ac.GHOST_LABEL}: On"
GHOST_OFF = f"{ac.GHOST_LABEL}: Off"


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    return await onboarded_world(bot_settings, redis, session_factory)


async def set_vip(sessions: Sessions, telegram_id: int, until: datetime | None) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == telegram_id).values(vip_until=until)
        )
        await session.commit()


async def set_ghost(sessions: Sessions, telegram_id: int, on: bool) -> None:
    async with sessions() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == telegram_id).values(ghost_mode=on)
        )
        await session.commit()


async def ghost_mode(sessions: Sessions, telegram_id: int) -> bool:
    async with sessions() as session:
        return bool(
            await session.scalar(
                select(User.ghost_mode).where(User.telegram_user_id == telegram_id)
            )
        )


def soon() -> datetime:
    return datetime.now(UTC) + timedelta(days=30)


def ended() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=1)


async def public_id(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await users.get_profile(session, user.id)).public_id


async def alice_visited_bob(world: World) -> bool:
    """Whether Alice's visit to Bob is recorded."""
    alice, bob = await world.user_id(USER_ID), await world.user_id(BOB_ID)
    async with world.sessions() as session:
        found = await session.scalar(
            select(ProfileVisit.visitor_user_id).where(
                ProfileVisit.visitor_user_id == alice, ProfileVisit.visited_user_id == bob
            )
        )
    return found is not None


def visit_notices_to_bob(world: World) -> int:
    return sum(1 for m in world.alice.session.sent_to(BOB_ID) if text_of(m).startswith("👀"))


# --- Each way Alice opens Bob's card ------------------------------------------------------


async def past_chats(world: World) -> None:
    await match(world)
    await world.alice.send(END_CHAT)
    await world.alice.send("/profile")
    await world.alice.press(pp.PEOPLE_BUTTON)
    await world.alice.press(pp.PAST_CHATS_BUTTON)
    [entry] = buttons(last_to(world.alice.session, USER_ID))
    await world.alice.press(entry)


async def visited_you(world: World) -> None:
    # Bob visits Alice first, so he is in her Visited you.
    await world.bob.send(f"/userid {await public_id(world.sessions, USER_ID)}")
    await world.alice.press_data(
        PeopleAction(action="visitor", user=await world.user_id(BOB_ID)).pack()
    )


async def liked_you(world: World) -> None:
    await world.bob.press_data(
        LikeAction(action="like", profile=await public_id(world.sessions, USER_ID)).pack()
    )
    await world.alice.press_data(
        PeopleAction(action="liker", user=await world.user_id(BOB_ID)).pack()
    )


async def you_liked(world: World) -> None:
    await world.alice.press_data(
        LikeAction(action="like", profile=await public_id(world.sessions, BOB_ID)).pack()
    )
    await world.alice.press_data(
        PeopleAction(action="liked", user=await world.user_id(BOB_ID)).pack()
    )


async def userid(world: World) -> None:
    await world.alice.send(f"/userid {await public_id(world.sessions, BOB_ID)}")


async def partner_card(world: World) -> None:
    await match(world)
    await world.alice.send(PARTNER_PROFILE)


OPENS: list[Callable[[World], Awaitable[None]]] = [
    past_chats,
    visited_you,
    liked_you,
    you_liked,
    userid,
    partner_card,
]


@pytest.mark.parametrize("open_card", OPENS, ids=lambda f: f.__name__)
async def test_a_ghost_visit_is_neither_recorded_nor_announced(
    world: World, open_card: Callable[[World], Awaitable[None]]
) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    await open_card(world)
    assert buttons(last_to(world.alice.session, USER_ID))  # Bob's card was shown
    assert not await alice_visited_bob(world)
    assert visit_notices_to_bob(world) == 0


@pytest.mark.parametrize("open_card", OPENS, ids=lambda f: f.__name__)
async def test_visits_are_recorded_once_vip_ends(
    world: World, open_card: Callable[[World], Awaitable[None]]
) -> None:
    await set_vip(world.sessions, USER_ID, ended())
    await open_card(world)
    assert await alice_visited_bob(world)


async def test_visits_are_recorded_and_announced_with_ghost_mode_off(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    await set_ghost(world.sessions, USER_ID, False)
    await userid(world)
    assert await alice_visited_bob(world)
    assert visit_notices_to_bob(world) == 1


# --- The switch -----------------------------------------------------------------------


async def open_settings(world: World) -> list[str]:
    await world.alice.send("/profile")
    assert await world.alice.press(ac.ADVANCED_SETTINGS) == ac.ADVANCED_SETTINGS_TEXT
    return buttons(last_to(world.alice.session, USER_ID))


def edited_buttons(world: World) -> list[str]:
    edits = [m for m in world.alice.session.requests if isinstance(m, EditMessageReplyMarkup)]
    markup = edits[-1].reply_markup
    assert markup is not None
    return [button.text for row in markup.inline_keyboard for button in row]


async def test_only_a_vip_sees_the_switch(world: World) -> None:
    assert GHOST_ON not in await open_settings(world)
    await set_vip(world.sessions, USER_ID, soon())
    shown = await open_settings(world)
    # After the notification settings, before deletion.
    assert shown.index(GHOST_ON) == shown.index(ac.DELETE_BUTTON) - 1


async def test_a_vip_switches_ghost_mode_off_and_on(world: World) -> None:
    await set_vip(world.sessions, USER_ID, soon())
    await open_settings(world)
    await world.alice.press(GHOST_ON)
    assert world.alice.session.callback_answers()[-1] == ac.GHOST_OFF
    assert GHOST_OFF in edited_buttons(world)
    assert not await ghost_mode(world.sessions, USER_ID)
    await world.alice.press_data(ac.GhostAction(on=True).pack())
    assert world.alice.session.callback_answers()[-1] == ac.GHOST_ON
    assert GHOST_ON in edited_buttons(world)
    assert await ghost_mode(world.sessions, USER_ID)


async def test_a_switch_without_vip_changes_nothing(world: World) -> None:
    # An old button from a VIP that has since ended.
    await set_vip(world.sessions, USER_ID, soon())
    await open_settings(world)
    await set_vip(world.sessions, USER_ID, ended())
    await world.alice.press(GHOST_ON)
    assert world.alice.session.callback_answers()[-1] == ac.GHOST_VIP_ONLY
    assert await ghost_mode(world.sessions, USER_ID)
    # The row is taken away.
    assert not any(b.startswith(ac.GHOST_LABEL) for b in edited_buttons(world))


async def test_vip_lists_ghost_mode() -> None:
    assert bot_vip.PERK_GHOST in bot_vip.perk_lines(VipPass(780, 180, 0))
