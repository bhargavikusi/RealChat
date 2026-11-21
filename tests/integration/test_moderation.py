"""Moderation actions: warnings, timed suspensions, bans, lifting them, and the notices
they owe (D-071)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import account as ac
from apps.bot import moderation as bot_moderation
from apps.bot.moderation import ModerationNotices
from backend.config.settings import BotSettings
from backend.modules.admin.models import AdminAuditEntry
from backend.modules.chat import service as chat_service
from backend.modules.chat.models import Conversation
from backend.modules.moderation import actions
from backend.modules.moderation.actions import NOTICE_ATTEMPTS, NOTICE_RETRY, Kind, Refusal
from backend.modules.moderation.models import ModerationAction, ModerationNotice, Report
from backend.modules.users import service as users
from backend.modules.users.models import User
from tests.support.chat import USER_ID
from tests.support.db import wait_until_blocked
from tests.support.world import (
    BOB_ID,
    World,
    assert_chat_ended,
    buttons,
    match,
    onboarded_world,
    text_of,
)

Sessions = async_sessionmaker[AsyncSession]
MODERATOR_ID = 7101
ADMIN_ID = 7102
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
async def world(bot_settings: BotSettings, redis: Redis, session_factory: Sessions) -> World:
    return await onboarded_world(bot_settings, redis, session_factory)


async def _staff(sessions: Sessions, telegram_id: int, role: str) -> int:
    """An account with this role, committed."""
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        user.role = role
        await session.commit()
        return user.id


async def _ids(world: World) -> tuple[int, int, int, int]:
    """Alice, Bob, a moderator, and an admin."""
    return (
        await world.user_id(USER_ID),
        await world.user_id(BOB_ID),
        await _staff(world.sessions, MODERATOR_ID, "moderator"),
        await _staff(world.sessions, ADMIN_ID, "admin"),
    )


async def _report(sessions: Sessions, reporter: int, reported: int) -> int:
    """A profile report by `reporter` about `reported`, filed directly."""
    async with sessions() as session:
        report = Report(
            reporter_user_id=reporter, reported_user_id=reported, reason="spam", urgent=False
        )
        session.add(report)
        await session.commit()
        return report.id


async def _act(
    sessions: Sessions, actor: int, target: int, kind: Kind, now: datetime = NOW, **kwargs: object
) -> actions.Outcome:
    async with sessions() as session:
        outcome = await actions.act(session, actor, target, kind, now, **kwargs)  # type: ignore[arg-type]
        await session.commit()
        return outcome


async def _user(sessions: Sessions, user_id: int) -> User:
    async with sessions() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user


async def _notices(sessions: Sessions) -> list[tuple[str, int]]:
    async with sessions() as session:
        rows = await session.execute(
            select(ModerationNotice.kind, ModerationNotice.recipient_user_id).order_by(
                ModerationNotice.id
            )
        )
        return [(kind, recipient) for kind, recipient in rows.all()]


async def _audit(sessions: Sessions) -> list[tuple[str, str | None, str | None]]:
    async with sessions() as session:
        rows = await session.execute(
            select(AdminAuditEntry.action, AdminAuditEntry.old_value, AdminAuditEntry.new_value)
            .where(AdminAuditEntry.action != "role_changed")
            .order_by(AdminAuditEntry.id)
        )
        return [(action, old, new) for action, old, new in rows.all()]


def _notices_job(world: World, bot_settings: BotSettings) -> ModerationNotices:
    return ModerationNotices(world.worker_bot, world.sessions, bot_settings)


async def test_a_warning_closes_the_report_and_owes_two_notices(world: World) -> None:
    alice, bob, moderator, _ = await _ids(world)
    report_id = await _report(world.sessions, alice, bob)

    outcome = await _act(
        world.sessions, moderator, bob, Kind.WARN, report_id=report_id, rule="harassment"
    )

    assert outcome.applied
    async with world.sessions() as session:
        report = await session.get(Report, report_id)
        assert report is not None
        assert (report.status, report.outcome, report.closed_by_action_id) == (
            "closed",
            "action_taken",
            outcome.action_id,
        )
        action = await session.get(ModerationAction, outcome.action_id)
        assert action is not None
        assert (action.kind, action.rule, action.actor_user_id) == ("warn", "harassment", moderator)
    assert (await _user(world.sessions, bob)).status == "active"
    assert await _notices(world.sessions) == [("action", bob), ("reporter", alice)]
    assert await _audit(world.sessions) == [("account_warned", "active", "harassment")]


async def test_closing_with_no_action_tells_only_the_reporter(world: World) -> None:
    alice, bob, moderator, _ = await _ids(world)
    report_id = await _report(world.sessions, alice, bob)

    assert (await _act(world.sessions, moderator, bob, Kind.CLOSE, report_id=report_id)).applied

    async with world.sessions() as session:
        report = await session.get(Report, report_id)
        assert report is not None and report.outcome == "no_action"
    assert await _notices(world.sessions) == [("reporter", alice)]
    assert await _audit(world.sessions) == [("report_closed", "active", f"report {report_id}")]


async def test_a_report_is_closed_by_one_action_only(world: World) -> None:
    alice, bob, moderator, admin = await _ids(world)
    report_id = await _report(world.sessions, alice, bob)
    assert (await _act(world.sessions, moderator, bob, Kind.CLOSE, report_id=report_id)).applied

    again = await _act(world.sessions, admin, bob, Kind.WARN, report_id=report_id, rule="spam")

    assert again.refusal is Refusal.REPORT_CLOSED
    assert await _notices(world.sessions) == [("reporter", alice)]


async def test_simultaneous_actions_on_one_report_apply_once(world: World) -> None:
    alice, bob, moderator, admin = await _ids(world)
    report_id = await _report(world.sessions, alice, bob)
    async with world.sessions() as holding:
        first = await actions.act(
            holding, moderator, bob, Kind.WARN, NOW, report_id=report_id, rule="spam"
        )
        assert first.applied
        task = asyncio.create_task(
            _act(world.sessions, admin, bob, Kind.BAN, report_id=report_id, rule="spam")
        )
        await wait_until_blocked(world.sessions)
        assert not task.done()  # waits for the first action's lock on Bob
        await holding.commit()
    assert (await task).refusal is Refusal.REPORT_CLOSED
    assert (await _user(world.sessions, bob)).status == "active"


async def test_only_moderators_and_admins_can_act(world: World) -> None:
    alice, bob, moderator, _ = await _ids(world)
    assert (await _act(world.sessions, alice, bob, Kind.WARN, rule="spam")).refusal is (
        Refusal.NOT_ALLOWED
    )
    # A restricted moderator can't act either.
    await _act(
        world.sessions,
        await _staff(world.sessions, 7103, "admin"),
        moderator,
        Kind.BAN,
        rule="other",
    )
    assert (await _act(world.sessions, moderator, bob, Kind.WARN, rule="spam")).refusal is (
        Refusal.NOT_ALLOWED
    )
    assert await _notices(world.sessions) == [("action", moderator)]


async def test_staff_are_protected_from_moderators_and_themselves(world: World) -> None:
    _, _, moderator, admin = await _ids(world)
    other_moderator = await _staff(world.sessions, 7104, "moderator")
    for actor, target in ((moderator, other_moderator), (moderator, admin), (admin, admin)):
        outcome = await _act(world.sessions, actor, target, Kind.WARN, rule="spam")
        assert outcome.refusal is Refusal.PROTECTED
    # An admin can act on a moderator.
    assert (await _act(world.sessions, admin, moderator, Kind.WARN, rule="spam")).applied


async def test_only_admins_lift_bans(world: World) -> None:
    _, bob, moderator, admin = await _ids(world)
    assert (await _act(world.sessions, moderator, bob, Kind.BAN, rule="threats")).applied

    assert (await _act(world.sessions, moderator, bob, Kind.LIFT)).refusal is Refusal.ADMIN_ONLY
    assert (await _user(world.sessions, bob)).status == "banned"
    assert (await _act(world.sessions, admin, bob, Kind.LIFT)).applied
    assert (await _user(world.sessions, bob)).status == "active"


async def test_moderators_lift_suspensions(world: World) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="spam", days=7)

    assert (await _act(world.sessions, moderator, bob, Kind.LIFT)).applied
    user = await _user(world.sessions, bob)
    assert (user.status, user.suspended_until) == ("active", None)
    assert (await _act(world.sessions, moderator, bob, Kind.LIFT)).refusal is (
        Refusal.NOT_RESTRICTED
    )


async def test_a_banned_account_cannot_be_suspended_or_banned_again(world: World) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.BAN, rule="threats")
    for kind, days in ((Kind.SUSPEND, 3), (Kind.BAN, None)):
        outcome = await _act(world.sessions, moderator, bob, kind, rule="spam", days=days)
        assert outcome.refusal is Refusal.BANNED
    # A warning still applies, e.g. for the record.
    assert (await _act(world.sessions, moderator, bob, Kind.WARN, rule="spam")).applied


async def test_a_new_suspension_replaces_the_end_time(world: World) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="spam", days=30)
    await _act(
        world.sessions, moderator, bob, Kind.SUSPEND, NOW + timedelta(days=1), rule="spam", days=1
    )
    assert (await _user(world.sessions, bob)).suspended_until == NOW + timedelta(days=2)


@pytest.mark.parametrize(
    ("kind", "kwargs"),
    [
        (Kind.WARN, {}),
        (Kind.SUSPEND, {"rule": "spam"}),
        (Kind.SUSPEND, {"rule": "spam", "days": 2}),
        (Kind.BAN, {"rule": "made_up"}),
        (Kind.LIFT, {"rule": "spam"}),
        (Kind.CLOSE, {}),
    ],
)
async def test_arguments_must_fit_the_action(
    world: World, kind: Kind, kwargs: dict[str, object]
) -> None:
    _, bob, moderator, _ = await _ids(world)
    with pytest.raises(ValueError):
        await _act(world.sessions, moderator, bob, kind, NOW, **kwargs)


async def test_suspending_ends_the_chat_and_the_worker_tells_everyone(
    world: World, bot_settings: BotSettings
) -> None:
    alice, bob, moderator, _ = await _ids(world)
    await match(world)

    outcome = await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="harassment", days=3)

    assert outcome.applied
    async with world.sessions() as session:
        assert await chat_service.active_chat(session, alice) is None
        ended = await session.scalar(select(Conversation.end_reason))
        assert ended == "restricted"
    assert await _notices(world.sessions) == [("action", bob), ("chat_ended", alice)]

    assert await _notices_job(world, bot_settings).tick(NOW) == 2
    assert_chat_ended(world.worker, USER_ID, bot_moderation.PARTNER_REMOVED)
    notice = world.worker.sent_to(BOB_ID)[-1]  # after the match announcement
    assert isinstance(notice, SendMessage)
    assert notice.text == (
        "⛔ Your account is suspended until 23 November 2026, 12:00 UTC for breaking "
        "RealChat's rules.\n\nRule: Harassment, hate, or bullying\n\n"
        "Until then, you can still read /help and the /terms, get help with a purchase "
        "with /paysupport, or delete your account.\n\n"
        "If you think this is a mistake, you can appeal by contacting support (see /help)."
    )
    # Sent once: a second tick finds nothing due.
    assert await _notices_job(world, bot_settings).tick(NOW + NOTICE_RETRY) == 0


async def test_the_reporter_is_never_told_which_action(
    world: World, bot_settings: BotSettings
) -> None:
    alice, bob, moderator, _ = await _ids(world)
    # Three reporters, one report each (profile reports are one per pair).
    reporters = {USER_ID: alice, 7105: 0, 7106: 0}
    for telegram_id in (7105, 7106):
        reporters[telegram_id] = await _staff(world.sessions, telegram_id, "user")
    for (telegram_id, reporter), (kind, rule) in zip(
        reporters.items(),
        ((Kind.CLOSE, None), (Kind.WARN, "spam"), (Kind.SUSPEND, "spam")),
        strict=True,
    ):
        report_id = await _report(world.sessions, reporter, bob)
        days = 1 if kind is Kind.SUSPEND else None
        outcome = await _act(
            world.sessions, moderator, bob, kind, report_id=report_id, rule=rule, days=days
        )
        assert outcome.applied, telegram_id

    await _notices_job(world, bot_settings).tick(NOW)

    told = {
        telegram_id: [
            m.text for m in world.worker.sent_to(telegram_id) if isinstance(m, SendMessage)
        ]
        for telegram_id in reporters
    }
    assert told == {
        USER_ID: [bot_moderation.REVIEWED],
        7105: [bot_moderation.REVIEWED_ACTION],
        7106: [bot_moderation.REVIEWED_ACTION],
    }


async def test_the_action_notice_offers_support_when_configured(
    world: World, bot_settings: BotSettings
) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.WARN, rule="spam")
    settings = bot_settings.model_copy(update={"support_contact": "https://t.me/realchat_support"})

    await ModerationNotices(world.worker_bot, world.sessions, settings).tick(NOW)

    [notice] = world.worker.sent_to(BOB_ID)
    assert isinstance(notice, SendMessage)
    assert notice.text.startswith("⚠️ You've received a warning")
    assert notice.text.endswith("contacting support with the button below.")
    assert notice.reply_markup is not None


async def test_a_notice_that_cannot_be_delivered_is_given_up(
    world: World, bot_settings: BotSettings
) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.WARN, rule="spam")
    world.worker.send_error = TelegramForbiddenError(
        method=SendMessage(chat_id=BOB_ID, text="x"),
        message="Forbidden: bot was blocked by the user",
    )

    assert await _notices_job(world, bot_settings).tick(NOW) == 1  # done with, not retried
    async with world.sessions() as session:
        assert await session.scalar(select(ModerationNotice.sent_at)) == NOW


async def test_a_failed_notice_is_retried_a_few_times(
    world: World, bot_settings: BotSettings
) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.WARN, rule="spam")
    world.worker.send_error = TelegramBadRequest(
        method=SendMessage(chat_id=BOB_ID, text="x"), message="Bad Request: try again"
    )
    job = _notices_job(world, bot_settings)
    for attempt in range(NOTICE_ATTEMPTS):
        assert await job.tick(NOW + attempt * NOTICE_RETRY) == 0
        # Not due again until the retry time.
        assert await job.tick(NOW + attempt * NOTICE_RETRY) == 0
        assert len(world.worker.failed_sends) == attempt + 1
    world.worker.send_error = None
    assert await job.tick(NOW + NOTICE_ATTEMPTS * NOTICE_RETRY) == 0  # given up
    assert len(world.worker.failed_sends) == NOTICE_ATTEMPTS


async def test_a_suspension_ends_by_itself_in_the_worker(
    world: World, bot_settings: BotSettings
) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="spam", days=1)
    job = _notices_job(world, bot_settings)

    await job.tick(NOW + timedelta(hours=23))
    assert (await _user(world.sessions, bob)).status == "suspended"
    await job.tick(NOW + timedelta(days=1))
    user = await _user(world.sessions, bob)
    assert (user.status, user.suspended_until) == ("active", None)


async def test_the_gate_shows_the_end_and_lifts_an_ended_suspension(
    world: World, bot_settings: BotSettings
) -> None:
    alice, _, moderator, _ = await _ids(world)
    now = datetime.now(UTC)
    await _act(world.sessions, moderator, alice, Kind.SUSPEND, now, rule="spam", days=1)
    until = (await _user(world.sessions, alice)).suspended_until
    assert until is not None

    assert await world.alice.send("/profile") == ac.restriction_notice("suspended", until)
    assert world.alice.last().startswith(
        f"Your account is suspended until {until.day} {until:%B %Y}"
    )

    async with world.sessions() as session:
        await session.execute(
            update(User).where(User.id == alice).values(suspended_until=now - timedelta(seconds=1))
        )
        await session.commit()
    reply = await world.alice.send("/profile")
    assert "suspended" not in reply
    assert (await _user(world.sessions, alice)).status == "active"


async def test_lifting_a_deleted_accounts_ban_leaves_it_deleted(world: World) -> None:
    alice, bob, moderator, admin = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.BAN, rule="spam")
    async with world.sessions() as session:
        await users.delete_account(session, bob, NOW)
        await session.commit()
    assert (await _user(world.sessions, bob)).status == "banned"

    assert (await _act(world.sessions, admin, bob, Kind.LIFT)).applied
    assert (await _user(world.sessions, bob)).status == "deleted"
    # Alice, who never deleted, goes back to active.
    await _act(world.sessions, moderator, alice, Kind.SUSPEND, rule="spam", days=1)
    await _act(world.sessions, moderator, alice, Kind.LIFT)
    assert (await _user(world.sessions, alice)).status == "active"


async def test_actions_are_kept_after_the_account_is_deleted(world: World) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.WARN, rule="spam")
    async with world.sessions() as session:
        await users.delete_account(session, bob, NOW)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(ModerationAction)) == 1


async def test_restriction_notices_offer_deletion_and_support(
    world: World, bot_settings: BotSettings
) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.BAN, rule="spam")
    settings = bot_settings.model_copy(update={"support_contact": "https://t.me/realchat_support"})

    await ModerationNotices(world.worker_bot, world.sessions, settings).tick(NOW)

    notice = world.worker.sent_to(BOB_ID)[-1]
    assert buttons(notice) == [ac.DELETE_BUTTON, bot_moderation.SUPPORT_BUTTON]
    # The Delete button is the one the restriction notice offers.
    assert isinstance(notice.reply_markup, InlineKeyboardMarkup)
    delete = notice.reply_markup.inline_keyboard[0][0]
    assert delete.callback_data == ac.AccountAction(action="delete").pack()


async def test_a_lift_notice_has_no_buttons(world: World, bot_settings: BotSettings) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="spam", days=1)
    await _act(world.sessions, moderator, bob, Kind.LIFT)
    settings = bot_settings.model_copy(update={"support_contact": "https://t.me/realchat_support"})

    await ModerationNotices(world.worker_bot, world.sessions, settings).tick(NOW)

    lifted = world.worker.sent_to(BOB_ID)[-1]
    assert (text_of(lifted), lifted.reply_markup) == (bot_moderation.LIFTED, None)


async def test_the_partner_notice_is_not_repeated_when_a_follow_up_fails(
    world: World, bot_settings: BotSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bob, moderator, _ = await _ids(world)
    await match(world)
    await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="spam", days=1)

    async def failing_menu(*args: object, **kwargs: object) -> None:
        raise TelegramBadRequest(method=SendMessage(chat_id=USER_ID, text="x"), message="nope")

    monkeypatch.setattr(bot_moderation, "send_menu", failing_menu)
    job = _notices_job(world, bot_settings)
    assert await job.tick(NOW) == 2  # both notices done with
    assert await job.tick(NOW + NOTICE_RETRY) == 0
    sent = [text_of(m) for m in world.worker.sent_to(USER_ID)]
    assert sent.count(bot_moderation.PARTNER_REMOVED) == 1


async def test_a_failed_partner_notice_is_retried_whole(
    world: World, bot_settings: BotSettings
) -> None:
    _, bob, moderator, _ = await _ids(world)
    await match(world)
    await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="spam", days=1)
    world.worker.send_error = TelegramBadRequest(
        method=SendMessage(chat_id=USER_ID, text="x"), message="Bad Request: try again"
    )
    job = _notices_job(world, bot_settings)
    assert await job.tick(NOW) == 0
    world.worker.send_error = None

    assert await job.tick(NOW + NOTICE_RETRY) == 2
    assert_chat_ended(world.worker, USER_ID, bot_moderation.PARTNER_REMOVED)


async def test_an_ended_suspension_lifted_during_deletion_stays_deleted(world: World) -> None:
    _, bob, moderator, _ = await _ids(world)
    await _act(world.sessions, moderator, bob, Kind.SUSPEND, rule="spam", days=1)
    later = NOW + timedelta(days=2)
    async with world.sessions() as deleting:
        await users.delete_account(deleting, bob, later)  # holds Bob's row

        async def lift() -> bool:
            async with world.sessions() as session:
                lifted = await actions.lift_if_expired(session, bob, later)
                await session.commit()
                return lifted

        task = asyncio.create_task(lift())
        await wait_until_blocked(world.sessions)
        assert not task.done()  # waits for the deletion's lock
        await deleting.commit()
    assert await task
    assert (await _user(world.sessions, bob)).status == "deleted"
