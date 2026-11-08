"""Chat requests (D-058): sending, charging on delivery, answers, connecting, and the
worker's expiry and lapse."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import chat as ch
from apps.bot import chat_requests as cr
from apps.bot.endings import BLOCK_BUTTON, PARTNER_ENDED, RequestAction
from apps.bot.keyboards import END_CHAT, NEW_CHAT
from backend.config.settings import BotSettings
from backend.modules.admin.models import AppSetting
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.chat import service as chat
from backend.modules.chat.models import ConversationParticipant
from backend.modules.chat_requests import service as requests
from backend.modules.chat_requests.models import ChatRequest
from backend.modules.chat_requests.service import ConnectOutcome
from backend.modules.coins import service as coins
from backend.modules.coins.models import CoinHold, CoinTransaction
from backend.modules.users import service as users
from backend.modules.users.models import User
from backend.modules.users.service import Notification
from backend.modules.users.terms import TERMS_VERSION
from tests.support.chat import USER_ID, Chat, complete_onboarding
from tests.support.db import wait_until_blocked
from tests.support.world import (
    BOB_ID,
    MESSAGE,
    REQUEST,
    World,
    buttons,
    last_to,
    onboarded_world,
    own_buttons,
    search,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
CAROL_ID = 6464
ALICE = "Night Owl"
BOB = "Moon Child"


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    """Alice and Bob, onboarded and just active, who have never chatted."""
    return await onboarded_world(bot_settings, redis, session_factory)


@pytest.fixture
async def carol(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> Chat:
    carol = Chat(bot_settings, redis, session_factory, user_id=CAROL_ID)
    await complete_onboarding(carol)
    return carol


async def public_id(sessions: Sessions, telegram_id: int) -> str:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        return (await users.get_profile(session, user.id)).public_id


async def user_id(sessions: Sessions, telegram_id: int) -> int:
    async with sessions() as session:
        return (await users.get_or_create_user(session, telegram_id)).id


async def ask(chat_: Chat, sessions: Sessions, telegram_id: int) -> None:
    """Tap 💬 Chat request on `telegram_id`'s card."""
    profile = await public_id(sessions, telegram_id)
    await chat_.press_data(RequestAction(action="ask", profile=profile).pack())


async def request(chat_: Chat, sessions: Sessions, telegram_id: int) -> None:
    """Ask, then Send."""
    await ask(chat_, sessions, telegram_id)
    await chat_.press_data(own_buttons(chat_)[cr.SEND_BUTTON])


def answer(chat_: Chat) -> str:
    return chat_.session.callback_answers()[-1]


def own_text(chat_: Chat) -> str:
    return text_of(last_to(chat_.session, chat_.user_id))


def notice_button(sender: Chat, button: str) -> str:
    """A button of the latest request notice the sender's bot delivered."""
    return sender.session.last_buttons()[button]


async def balance(sessions: Sessions, telegram_id: int) -> int:
    async with sessions() as session:
        return (await coins.balance(session, await user_id(sessions, telegram_id))).available


async def charges(sessions: Sessions) -> int:
    async with sessions() as session:
        found = await session.scalar(
            select(func.count())
            .select_from(CoinTransaction)
            .where(CoinTransaction.kind == "chat_request")
        )
        return int(found or 0)


async def statuses(sessions: Sessions) -> list[str]:
    async with sessions() as session:
        return list(await session.scalars(select(ChatRequest.status).order_by(ChatRequest.id)))


async def hold_statuses(sessions: Sessions) -> list[str]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(CoinHold.status)
                .where(CoinHold.purpose == "chat_request")
                .order_by(CoinHold.id)
            )
        )


async def open_partner(sessions: Sessions, telegram_id: int) -> int | None:
    """The internal id of the user's current chat partner, if any."""
    async with sessions() as session:
        active = await chat.active_chat(session, await user_id(sessions, telegram_id))
        return None if active is None else active.partner_id


async def chat_between(sessions: Sessions, first: int, second: int) -> None:
    async with sessions() as session:
        await chat.start_conversation(
            session, await user_id(sessions, first), await user_id(sessions, second)
        )
        await session.commit()


async def set_setting(sessions: Sessions, key: SettingKey, value: int) -> None:
    async with sessions() as session:
        session.add(AppSetting(key=key.value, value=value))
        await session.commit()


async def move_back(sessions: Sessions, column: str, by: timedelta) -> None:
    async with sessions() as session:
        await session.execute(
            update(ChatRequest).values({column: getattr(ChatRequest, column) - by})
        )
        await session.commit()


def jobs(sessions: Sessions, chat_: Chat) -> requests.RequestJobs:
    """The worker's job, sending its notices through `chat_`'s bot."""
    return requests.RequestJobs(
        sessions, RuntimeSettings(ttl_seconds=0), cr.RequestNotices(chat_.bot, sessions)
    )


# --- Sending and charging -----------------------------------------------------------------


async def test_cards_offer_a_chat_request(world: World, session_factory: Sessions) -> None:
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    card = buttons(last_to(world.alice.session, USER_ID))
    assert card.index(REQUEST) == card.index(MESSAGE) + 1


async def test_a_request_states_its_price_then_is_delivered_and_charged_once(
    world: World, session_factory: Sessions
) -> None:
    before = await balance(session_factory, USER_ID)
    await ask(world.alice, session_factory, BOB_ID)
    assert own_text(world.alice) == cr.CONFIRM.format(nickname=BOB, price="2 coins")
    assert await statuses(session_factory) == []  # nothing until Send
    await world.alice.press_data(own_buttons(world.alice)[cr.SEND_BUTTON])
    alice = await public_id(session_factory, USER_ID)
    notice = last_to(world.alice.session, BOB_ID)
    assert text_of(notice) == (
        f"💬 {ALICE} · /userid_{alice} wants to chat with you. Answer within 60 minutes."
    )
    assert buttons(notice) == [cr.ACCEPT_BUTTON, cr.DECLINE_BUTTON]
    assert own_text(world.alice) == cr.SENT.format(nickname=BOB, price="2 coins")
    assert await balance(session_factory, USER_ID) == before - 2
    assert await charges(session_factory) == 1
    assert await statuses(session_factory) == ["delivered"]


async def test_a_double_tap_on_send_sends_and_charges_once(
    world: World, session_factory: Sessions
) -> None:
    await ask(world.alice, session_factory, BOB_ID)
    data = own_buttons(world.alice)[cr.SEND_BUTTON]
    await world.alice.press_data(data)
    await world.alice.press_data(data)
    assert answer(world.alice) == cr.ALREADY_SENT
    assert await charges(session_factory) == 1
    notices = [m for m in world.alice.session.sent_to(BOB_ID) if "wants to chat" in text_of(m)]
    assert len(notices) == 1


async def test_a_failed_notice_releases_the_coins(world: World, session_factory: Sessions) -> None:
    before = await balance(session_factory, USER_ID)
    telegram = world.alice.session
    record = telegram.make_request

    async def refusing(bot: Any, method: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage) and "wants to chat" in method.text:
            telegram.requests.append(method)
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        return await record(bot, method, timeout)

    telegram.make_request = refusing  # type: ignore[method-assign]
    await request(world.alice, session_factory, BOB_ID)
    assert own_text(world.alice) == cr.NOT_DELIVERED.format(nickname=BOB)
    assert await balance(session_factory, USER_ID) == before
    assert await charges(session_factory) == 0
    assert await statuses(session_factory) == ["failed"]
    assert await hold_statuses(session_factory) == ["released"]


async def test_the_price_shown_is_the_price_charged(
    world: World, session_factory: Sessions
) -> None:
    before = await balance(session_factory, USER_ID)
    await ask(world.alice, session_factory, BOB_ID)
    await set_setting(session_factory, SettingKey.CHAT_REQUEST_COINS, 5)
    world.alice.dispatcher["runtime_settings"].invalidate()
    await world.alice.press_data(own_buttons(world.alice)[cr.SEND_BUTTON])
    assert answer(world.alice) == cr.PRICE_CHANGED
    assert await balance(session_factory, USER_ID) == before
    assert await statuses(session_factory) == []


async def test_not_enough_coins_is_refused(world: World, session_factory: Sessions) -> None:
    await set_setting(session_factory, SettingKey.CHAT_REQUEST_COINS, 11)
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == cr.NO_COINS.format(price="11 coins", available="10 coins")


# --- Who can be asked -----------------------------------------------------------------------


async def _refused_with(world: World, session_factory: Sessions, text: str) -> None:
    before = await balance(session_factory, USER_ID)
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == text
    assert await statuses(session_factory) == []
    assert await balance(session_factory, USER_ID) == before


async def test_an_inactive_recipient_cannot_be_asked(
    world: World, session_factory: Sessions
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == BOB_ID)
            .values(last_active_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await session.commit()
    await _refused_with(world, session_factory, cr.NOT_NOW.format(nickname=BOB))


async def test_a_recipient_in_a_chat_cannot_be_asked(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await chat_between(session_factory, BOB_ID, CAROL_ID)
    await _refused_with(world, session_factory, cr.NOT_NOW.format(nickname=BOB))


async def test_silent_mode_stops_requests(world: World, session_factory: Sessions) -> None:
    async with session_factory() as session:
        await users.set_notification(
            session, await user_id(session_factory, BOB_ID), Notification.SILENT_MODE, True
        )
        await session.commit()
    await _refused_with(world, session_factory, cr.NOT_NOW.format(nickname=BOB))


async def test_a_searching_sender_must_stop_first(world: World, session_factory: Sessions) -> None:
    await search(world.alice)
    await _refused_with(world, session_factory, cr.REFUSALS[requests.Refusal.BUSY])


async def test_a_chatting_sender_must_end_first(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await chat_between(session_factory, USER_ID, CAROL_ID)
    await _refused_with(world, session_factory, cr.REFUSALS[requests.Refusal.BUSY])


async def test_someone_blocked_cannot_ask_and_sees_no_button(
    world: World, session_factory: Sessions
) -> None:
    await world.bob.send(f"/userid_{await public_id(session_factory, USER_ID)}")
    await world.bob.press_data(own_buttons(world.bob)[BLOCK_BUTTON])
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    assert REQUEST not in buttons(last_to(world.alice.session, USER_ID))
    await _refused_with(world, session_factory, cr.NOT_NOW.format(nickname=BOB))


async def test_the_blocker_must_unblock_first(world: World, session_factory: Sessions) -> None:
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    await _refused_with(world, session_factory, cr.REFUSALS[requests.Refusal.YOU_BLOCKED])


async def test_one_waiting_request_per_person(world: World, session_factory: Sessions) -> None:
    await request(world.alice, session_factory, BOB_ID)
    await ask(world.alice, session_factory, BOB_ID)
    assert answer(world.alice) == cr.REFUSALS[requests.Refusal.WAITING].format(nickname=BOB)
    assert await charges(session_factory) == 1


# --- Answers ----------------------------------------------------------------------------


async def test_decline_tells_the_sender_and_keeps_the_charge(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    after_send = await balance(session_factory, USER_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await world.bob.press_data(notice_button(world.alice, cr.DECLINE_BUTTON))
    assert answer(world.bob) == cr.DECLINED
    assert text_of(last_to(world.bob.session, USER_ID)) == cr.DECLINED_NOTICE.format(nickname=BOB)
    assert await statuses(session_factory) == ["declined"]
    assert await balance(session_factory, USER_ID) == after_send
    await world.bob.press_data(accept)
    assert answer(world.bob) == cr.GONE
    assert await open_partner(session_factory, USER_ID) is None


async def test_accepting_connects_free_users(world: World, session_factory: Sessions) -> None:
    await request(world.alice, session_factory, BOB_ID)
    await world.bob.press_data(notice_button(world.alice, cr.ACCEPT_BUTTON))
    assert await open_partner(session_factory, USER_ID) == await user_id(session_factory, BOB_ID)
    for telegram_id, partner in ((USER_ID, BOB), (BOB_ID, ALICE)):
        card = last_to(world.bob.session, telegram_id)
        assert text_of(card).startswith(f"{cr.CONNECTED}\n\n👤 {partner}")
    assert await statuses(session_factory) == ["connected"]
    assert await charges(session_factory) == 1
    # A second tap connects nobody again.
    await world.bob.press_data(notice_button(world.alice, cr.ACCEPT_BUTTON))
    assert answer(world.bob) == cr.GONE


async def test_concurrent_accepts_create_one_conversation(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    async with session_factory() as session:
        request_id = int(await session.scalar(select(ChatRequest.id)) or 0)
    since = datetime.now(UTC) - timedelta(hours=1)
    now = datetime.now(UTC)
    async with session_factory() as first, session_factory() as second:
        one = await requests.connect(first, request_id, "delivered", since, now, TERMS_VERSION)
        other = asyncio.create_task(
            requests.connect(second, request_id, "delivered", since, now, TERMS_VERSION)
        )
        await wait_until_blocked(session_factory)
        await first.commit()
        two = await other
        await second.commit()
    assert one.outcome == ConnectOutcome.CONNECTED
    assert two.outcome == ConnectOutcome.GONE
    async with session_factory() as session:
        open_count = await session.scalar(
            select(func.count())
            .select_from(ConversationParticipant)
            .where(ConversationParticipant.left_at.is_(None))
        )
    assert open_count == 2


async def test_an_unanswered_request_expires_and_the_sender_is_told(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    job = jobs(session_factory, world.bob)
    assert await job.tick(datetime.now(UTC)) == 0
    await move_back(session_factory, "delivered_at", timedelta(minutes=61))
    assert await job.tick(datetime.now(UTC)) == 1
    assert text_of(last_to(world.bob.session, USER_ID)) == cr.EXPIRED_NOTICE.format(nickname=BOB)
    assert await statuses(session_factory) == ["expired"]
    assert await charges(session_factory) == 1
    await world.bob.press_data(accept)
    assert answer(world.bob) == cr.GONE


async def test_an_expired_request_cannot_be_accepted_before_the_worker_runs(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    await move_back(session_factory, "delivered_at", timedelta(minutes=61))
    await world.bob.press_data(notice_button(world.alice, cr.ACCEPT_BUTTON))
    assert answer(world.bob) == cr.GONE
    assert await open_partner(session_factory, BOB_ID) is None


# --- Busy users ---------------------------------------------------------------------------


async def test_a_searching_recipient_confirms_and_their_search_ends(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await search(world.bob)
    await world.bob.press_data(accept)
    assert own_text(world.bob) == cr.CONFIRM_BUSY.format(what="search")
    assert await open_partner(session_factory, BOB_ID) is None
    await world.bob.press_data(own_buttons(world.bob)[cr.END_AND_ACCEPT])
    assert await open_partner(session_factory, BOB_ID) == await user_id(session_factory, USER_ID)
    assert (
        await world.bob.dispatcher["search_queue"].current_search(
            await user_id(session_factory, BOB_ID)
        )
        is None
    )


async def test_a_chatting_recipient_confirms_and_their_partner_is_told(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await chat_between(session_factory, BOB_ID, CAROL_ID)
    await world.bob.press_data(accept)
    assert own_text(world.bob) == cr.CONFIRM_BUSY.format(what="chat")
    await world.bob.press_data(own_buttons(world.bob)[cr.END_AND_ACCEPT])
    assert await open_partner(session_factory, BOB_ID) == await user_id(session_factory, USER_ID)
    assert await open_partner(session_factory, CAROL_ID) is None
    assert PARTNER_ENDED in [text_of(m) for m in world.bob.session.sent_to(CAROL_ID)]


async def test_a_raced_connection_changes_nothing(
    world: World,
    session_factory: Sessions,
    carol: Chat,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await chat_between(session_factory, BOB_ID, CAROL_ID)

    async def matched_meanwhile(*args: Any) -> int:
        raise IntegrityError("start", {}, Exception("uq_conversation_participants_open_user"))

    monkeypatch.setattr(chat, "start_conversation", matched_meanwhile)
    await world.bob.press_data(
        RequestAction.unpack(accept).model_copy(update={"confirm": 1}).pack()
    )
    assert answer(world.bob) == cr.RACED
    # Rolled back: Bob's chat with Carol was not ended.
    assert await open_partner(session_factory, BOB_ID) == await user_id(session_factory, CAROL_ID)
    assert await statuses(session_factory) == ["delivered"]


async def test_a_busy_sender_switches_and_their_paid_search_is_returned(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    before = await balance(session_factory, USER_ID)
    await world.alice.send(NEW_CHAT)
    await world.alice.press("👩 Women · 2 coins")
    assert await balance(session_factory, USER_ID) == before - 2
    await world.bob.press_data(accept)
    assert own_text(world.bob) == cr.WAITING_FOR.format(nickname=ALICE)
    assert text_of(last_to(world.bob.session, USER_ID)) == cr.SWITCH_PROMPT.format(
        nickname=BOB, what="search", window="90 seconds"
    )
    assert await statuses(session_factory) == ["accepted"]
    await world.alice.press_data(world.bob.session.last_buttons()[cr.SWITCH_BUTTON])
    assert await open_partner(session_factory, USER_ID) == await user_id(session_factory, BOB_ID)
    card = last_to(world.alice.session, USER_ID)
    assert text_of(card).startswith(f"{cr.CONNECTED}\n{ch.COINS_RETURNED}")
    assert await balance(session_factory, USER_ID) == before
    assert await statuses(session_factory) == ["connected"]


async def test_a_busy_sender_who_stays_lets_it_lapse(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await chat_between(session_factory, USER_ID, CAROL_ID)
    await world.bob.press_data(accept)
    switch = world.bob.session.last_buttons()[cr.SWITCH_BUTTON]
    await world.alice.press_data(world.bob.session.last_buttons()[cr.STAY_BUTTON])
    assert answer(world.alice) == cr.STAYED
    assert text_of(last_to(world.alice.session, BOB_ID)) == cr.LAPSED_NOTICE.format(nickname=ALICE)
    assert await statuses(session_factory) == ["lapsed"]
    await world.alice.press_data(switch)
    assert answer(world.alice) == cr.GONE
    assert await open_partner(session_factory, USER_ID) == await user_id(session_factory, CAROL_ID)


async def test_a_busy_sender_who_does_not_answer_lets_it_lapse(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await search(world.alice)
    await world.bob.press_data(accept)
    switch = world.bob.session.last_buttons()[cr.SWITCH_BUTTON]
    job = jobs(session_factory, world.alice)
    assert await job.tick(datetime.now(UTC)) == 0
    await move_back(session_factory, "accepted_at", timedelta(seconds=91))
    # The switch window is over even before the worker runs.
    await world.alice.press_data(switch)
    assert answer(world.alice) == cr.GONE
    assert await job.tick(datetime.now(UTC)) == 1
    assert text_of(last_to(world.alice.session, BOB_ID)) == cr.LAPSED_NOTICE.format(nickname=ALICE)
    assert await statuses(session_factory) == ["lapsed"]


async def test_the_first_acceptance_closes_the_senders_other_requests(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    bob_accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await carol.send("/profile")  # active
    await request(world.alice, session_factory, CAROL_ID)
    carol_accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await world.bob.press_data(bob_accept)
    await carol.press_data(carol_accept)
    assert answer(carol) == cr.GONE
    assert await statuses(session_factory) == ["connected", "closed"]
    assert await charges(session_factory) == 2


# --- Interruptions and deletion -----------------------------------------------------------


async def test_an_interrupted_send_is_failed_without_a_charge(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await user_id(session_factory, USER_ID), await user_id(session_factory, BOB_ID)
    now = datetime.now(UTC)
    async with session_factory() as session:
        sent = await requests.send(
            session, alice, bob, "k", 2, now - timedelta(minutes=10), now, sender_searching=False
        )
        await session.commit()
    assert sent.request_id is not None
    job = jobs(session_factory, world.alice)
    assert await job.tick(now) == 0
    assert await job.tick(now + requests.STALE_AFTER + timedelta(seconds=5)) == 1
    assert await statuses(session_factory) == ["failed"]
    assert await hold_statuses(session_factory) == ["released"]
    async with session_factory() as session:
        assert not await requests.delivered(session, sent.request_id, now)
        await session.commit()
    assert await charges(session_factory) == 0


async def test_deleting_the_recipient_returns_a_pending_requests_coins(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await user_id(session_factory, USER_ID), await user_id(session_factory, BOB_ID)
    now = datetime.now(UTC)
    async with session_factory() as session:
        await requests.send(
            session, alice, bob, "k", 2, now - timedelta(minutes=10), now, sender_searching=False
        )
        await session.commit()
    async with session_factory() as session:
        await users.delete_account(session, bob, now)
        await session.commit()
    assert await statuses(session_factory) == []
    assert await hold_statuses(session_factory) == ["released"]
    assert await balance(session_factory, USER_ID) == 10


async def test_a_deleted_sender_cannot_be_connected(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    async with session_factory() as session:
        await users.delete_account(
            session, await user_id(session_factory, USER_ID), datetime.now(UTC)
        )
        await session.commit()
    await world.bob.press_data(accept)
    assert answer(world.bob) == cr.GONE
    assert await open_partner(session_factory, BOB_ID) is None


async def test_a_suspended_sender_cannot_be_connected(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == USER_ID).values(status="suspended")
        )
        await session.commit()
    await world.bob.press_data(accept)
    assert answer(world.bob) == cr.UNAVAILABLE_NOW.format(nickname=ALICE)
    assert await statuses(session_factory) == ["closed"]
    assert await open_partner(session_factory, BOB_ID) is None


async def test_ending_the_connected_chat_works_as_usual(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    await world.bob.press_data(notice_button(world.alice, cr.ACCEPT_BUTTON))
    await world.alice.send(END_CHAT)
    assert await open_partner(session_factory, BOB_ID) is None


async def test_an_expired_request_offers_a_busy_recipient_nothing(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await search(world.bob)
    await move_back(session_factory, "delivered_at", timedelta(minutes=61))
    await world.bob.press_data(accept)
    assert answer(world.bob) == cr.GONE
    assert cr.CONFIRM_BUSY.format(what="search") not in world.bob.session.sent_texts()


async def test_a_block_after_sending_stops_the_connection(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await world.alice.send(f"/userid_{await public_id(session_factory, BOB_ID)}")
    await world.alice.press_data(own_buttons(world.alice)[BLOCK_BUTTON])
    await world.bob.press_data(accept)
    assert answer(world.bob) == cr.UNAVAILABLE_NOW.format(nickname=ALICE)
    assert await statuses(session_factory) == ["closed"]
    # The sender, who paid for it, is told it's over.
    assert text_of(last_to(world.bob.session, USER_ID)) == cr.NOT_CONNECTED.format(nickname=BOB)
    assert await open_partner(session_factory, BOB_ID) is None


# --- Review follow-ups ---------------------------------------------------------------------


async def test_concurrent_sends_of_one_draft_keep_its_hold(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await user_id(session_factory, USER_ID), await user_id(session_factory, BOB_ID)
    now = datetime.now(UTC)
    since = now - timedelta(minutes=10)
    async with session_factory() as first, session_factory() as second:
        one = await requests.send(first, alice, bob, "k", 2, since, now, sender_searching=False)
        other = asyncio.create_task(
            requests.send(second, alice, bob, "k", 2, since, now, sender_searching=False)
        )
        await wait_until_blocked(session_factory)
        await first.commit()
        two = await other
        await second.commit()
    # The account lock serializes the two: the second sees the first request waiting,
    # takes no hold of its own, and leaves the first one's hold alone.
    assert two.request_id is None and two.refusal == requests.Refusal.WAITING
    assert await hold_statuses(session_factory) == ["held"]
    assert one.request_id is not None
    async with session_factory() as session:
        assert await requests.delivered(session, one.request_id, now)
        await session.commit()
    assert await charges(session_factory) == 1


async def test_connecting_closes_a_request_still_being_sent_without_a_charge(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    alice, carol_id = (
        await user_id(session_factory, USER_ID),
        await user_id(session_factory, CAROL_ID),
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        in_flight = await requests.send(
            session,
            alice,
            carol_id,
            "k",
            2,
            now - timedelta(minutes=10),
            now,
            sender_searching=False,
        )
        await session.commit()
    assert in_flight.request_id is not None
    await world.bob.press_data(accept)
    assert await statuses(session_factory) == ["connected", "closed"]
    assert await hold_statuses(session_factory) == ["captured", "released"]
    async with session_factory() as session:
        assert not await requests.delivered(session, in_flight.request_id, now)
        await session.commit()
    assert await charges(session_factory) == 1


async def test_a_recipient_waiting_for_a_switch_is_told_when_the_sender_goes_elsewhere(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    bob_accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await carol.send("/profile")  # active
    await request(world.alice, session_factory, CAROL_ID)
    carol_accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await search(world.alice)
    await world.bob.press_data(bob_accept)
    await carol.press_data(carol_accept)
    assert await statuses(session_factory) == ["accepted", "accepted"]
    await world.alice.press_data(carol.session.last_buttons()[cr.SWITCH_BUTTON])
    assert await statuses(session_factory) == ["closed", "connected"]
    assert text_of(last_to(world.alice.session, BOB_ID)) == cr.LAPSED_NOTICE.format(nickname=ALICE)


async def test_the_sender_is_told_when_an_interrupted_send_fails(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await user_id(session_factory, USER_ID), await user_id(session_factory, BOB_ID)
    now = datetime.now(UTC)
    async with session_factory() as session:
        await requests.send(
            session, alice, bob, "k", 2, now - timedelta(minutes=10), now, sender_searching=False
        )
        await session.commit()
    await jobs(session_factory, world.bob).tick(now + requests.STALE_AFTER + timedelta(seconds=5))
    assert text_of(last_to(world.bob.session, USER_ID)) == cr.NOT_DELIVERED.format(nickname=BOB)


async def test_answers_during_delivery_can_be_repeated(
    world: World, session_factory: Sessions
) -> None:
    alice, bob = await user_id(session_factory, USER_ID), await user_id(session_factory, BOB_ID)
    now = datetime.now(UTC)
    async with session_factory() as session:
        sent = await requests.send(
            session, alice, bob, "k", 2, now - timedelta(minutes=10), now, sender_searching=False
        )
        await session.commit()
    assert sent.request_id is not None
    for action in ("accept", "decline"):
        await world.bob.press_data(RequestAction(action=action, request=sent.request_id).pack())
        assert answer(world.bob) == cr.RETRY
    assert await statuses(session_factory) == ["pending"]


async def test_one_failing_notice_does_not_stop_the_others(
    world: World, session_factory: Sessions, carol: Chat
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    await carol.send("/profile")  # active
    await request(world.alice, session_factory, CAROL_ID)
    await move_back(session_factory, "delivered_at", timedelta(minutes=61))
    told: list[int] = []

    class Flaky:
        async def failed(self, parties: requests.Parties) -> None: ...

        async def lapsed(self, parties: requests.Parties) -> None: ...

        async def expired(self, parties: requests.Parties) -> None:
            if not told:
                told.append(0)
                raise RuntimeError("database hiccup")
            told.append(parties.recipient_id)

    job = requests.RequestJobs(session_factory, RuntimeSettings(ttl_seconds=0), Flaky())
    assert await job.tick(datetime.now(UTC)) == 2
    assert len(told) == 2


async def test_a_waiting_recipient_is_told_when_the_switch_cannot_connect(
    world: World, session_factory: Sessions
) -> None:
    await request(world.alice, session_factory, BOB_ID)
    accept = notice_button(world.alice, cr.ACCEPT_BUTTON)
    await search(world.alice)
    await world.bob.press_data(accept)
    switch = world.bob.session.last_buttons()[cr.SWITCH_BUTTON]
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == BOB_ID).values(status="suspended")
        )
        await session.commit()
    await world.alice.press_data(switch)
    assert answer(world.alice) == cr.UNAVAILABLE_NOW.format(nickname=BOB)
    assert await statuses(session_factory) == ["closed"]
    assert text_of(last_to(world.alice.session, BOB_ID)) == cr.LAPSED_NOTICE.format(nickname=ALICE)
