"""What moderation actions tell people, as sent by the worker (D-071).

The account acted on is told the action and the rule, with how to appeal through the
support contact; the reporter is told only that the report was reviewed, and whether
action followed, never which; and the partner in a chat the action ended is told the
chat ended, without the reason. The job also lifts suspensions that have ended.

`StaffAlerts` sends moderators and admins their alerts: urgent reports and the daily
summary of open reports.
"""

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.account import DELETE_BUTTON, AccountAction
from apps.bot.endings import SAFETY_PROMPT, menu_price, safety_keyboard, send_menu
from apps.bot.keyboards import main_keyboard
from apps.bot.payments import vip_date
from apps.bot.relay import unreachable
from backend.config.settings import BotSettings
from backend.core.logging import exception_summary
from backend.modules.admin.settings import RuntimeSettings, SettingKey
from backend.modules.moderation import actions, alerts, service
from backend.modules.moderation.actions import DueNotice

logger = logging.getLogger(__name__)

NOTICE_BATCH = 50
LIFT_BATCH = 100

RULE_LABELS = {
    "spam": "Spam, scams, or advertising",
    "harassment": "Harassment, hate, or bullying",
    "sexual": "Sexual content without consent",
    "under_18": "Under 18 (RealChat is for adults only)",
    "threats": "Threats or violence",
    "impersonation": "Impersonation or a false age or identity",
    "private_info": "Sharing someone's private information",
    "evasion": "Getting around a restriction or abusing rewards or payments",
    "other": "Other breach of the User Agreement",
}
WARNED = (
    "⚠️ You've received a warning for breaking RealChat's rules.\n\n"
    "Rule: {rule}\n\n"
    "Please follow the User Agreement (/terms). Breaking the rules again can lead to a "
    "suspension or a ban."
)
SUSPENDED = (
    "⛔ Your account is suspended until {until} for breaking RealChat's rules.\n\n"
    "Rule: {rule}\n\n"
    "Until then, you can still read /help and the /terms, get help with a purchase with "
    "/paysupport, or delete your account."
)
BANNED = (
    "⛔ Your account has been banned for breaking RealChat's rules.\n\n"
    "Rule: {rule}\n\n"
    "You can still read /help and the /terms, get help with a purchase with /paysupport, "
    "or delete your account."
)
APPEAL = "If you think this is a mistake, you can appeal by contacting support{where}."
LIFTED = "✅ The restriction on your account has been lifted. Send /start to continue."
REVIEWED = "Thanks, we've reviewed your report."
REVIEWED_ACTION = "Thanks, we've reviewed your report and taken action."
PARTNER_REMOVED = "Your chat partner can't continue this chat, so it has ended."
SUPPORT_BUTTON = "💬 Support"


def action_notice(notice: DueNotice, has_support: bool) -> str:
    """The message for the account acted on."""
    if notice.action_kind == "lift":
        return LIFTED
    rule = RULE_LABELS[str(notice.rule)]
    if notice.action_kind == "warn":
        text = WARNED.format(rule=rule)
    elif notice.action_kind == "suspend" and notice.ends_at is not None:
        text = SUSPENDED.format(rule=rule, until=vip_date(notice.ends_at))
    else:
        text = BANNED.format(rule=rule)
    where = " with the button below" if has_support else " (see /help)"
    return f"{text}\n\n{APPEAL.format(where=where)}"


class ModerationNotices:
    """Sends the notices moderation actions owe, and lifts suspensions that have ended.
    Runs in the worker.

    A notice is marked sent once Telegram accepts it, or once it never can be (the person
    blocked the bot). Any other failure is tried again later (`actions.NOTICE_RETRY`), a
    few times at most, and only the failure is logged, without user data.
    """

    def __init__(
        self, bot: Bot, sessions: async_sessionmaker[AsyncSession], settings: BotSettings
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._support = settings.support_contact

    def _action_keyboard(self, action_kind: str) -> InlineKeyboardMarkup | None:
        """🗑 Delete account for a suspension or ban, as the restriction notice offers, and
        💬 Support to appeal when the contact is configured. A lift needs neither."""
        rows = []
        if action_kind in actions.RESTRICTING:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=DELETE_BUTTON, callback_data=AccountAction(action="delete").pack()
                    )
                ]
            )
        if self._support is not None and action_kind != "lift":
            rows.append([InlineKeyboardButton(text=SUPPORT_BUTTON, url=str(self._support))])
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    async def tick(self, now: datetime) -> int:
        """Lift ended suspensions, then send one batch of notices; returns how many
        notices were sent."""
        async with self._sessions() as session:
            lifted = await actions.lift_expired(session, now, LIFT_BATCH)
            await session.commit()
            if lifted:
                logger.info("lifted ended suspensions", extra={"count": lifted})
            claimed = await actions.claim_notices(session, now, NOTICE_BATCH)
            await session.commit()
            sent = 0
            for notice in claimed:
                if await self._send(session, notice):
                    await actions.mark_sent(session, notice.id, now)
                    await session.commit()
                    sent += 1
        return sent

    async def _send(self, session: AsyncSession, notice: DueNotice) -> bool:
        """Send one notice; True if it is done with (sent, or never deliverable)."""
        try:
            if notice.kind == "action":
                await self._bot.send_message(
                    notice.telegram_user_id,
                    action_notice(notice, self._support is not None),
                    reply_markup=self._action_keyboard(notice.action_kind),
                )
            elif notice.kind == "reporter":
                taken = notice.action_kind in actions.ON_ACCOUNT
                await self._bot.send_message(
                    notice.telegram_user_id, REVIEWED_ACTION if taken else REVIEWED
                )
            elif notice.conversation_id is not None:
                blocked = await service.is_blocked(
                    session, notice.recipient_user_id, notice.target_user_id
                )
                price = await menu_price(session)
                await session.commit()
                # Only this message is retried; the follow-ups are sent once, after it.
                await self._bot.send_message(
                    notice.telegram_user_id, PARTNER_REMOVED, reply_markup=main_keyboard()
                )
                await self._follow_up(
                    notice.telegram_user_id, notice.conversation_id, blocked, price
                )
        except TelegramAPIError as exc:
            logger.info("could not send a moderation notice", extra=exception_summary(exc))
            return unreachable(exc)
        return True

    async def _follow_up(
        self, chat_id: int, conversation_id: int, blocked: bool, price: int
    ) -> None:
        """Block and Report for the partner, then the new-chat menu, as after any ended
        chat (`send_chat_ended`), best effort: a failure here is logged, not retried, so
        the notice is never sent twice."""
        try:
            await self._bot.send_message(
                chat_id, SAFETY_PROMPT, reply_markup=safety_keyboard(conversation_id, blocked)
            )
        except TelegramAPIError as exc:
            logger.warning("could not offer block and report", extra=exception_summary(exc))
        try:
            await send_menu(self._bot, chat_id, price)
        except TelegramAPIError as exc:
            logger.warning("could not send the new-chat menu", extra=exception_summary(exc))


# --- Alerts to moderators and admins -------------------------------------------------

ALERT_BATCH = 50
URGENT_ALERT = "🚨 Urgent report: {reason} · filed {at} UTC.\n\nOpen it in 🛡 Moderation."
SUMMARY_ALERT = "📋 Open reports: {open}{urgent}.\n\nReview them in 🛡 Moderation."
OPEN_REPORT_BUTTON = "🛡 Open report"
OPEN_PANEL_BUTTON = "🛡 Open Moderation"
# The report reasons as the panel names them.
REASON_LABELS = {
    "spam": "Spam or scam",
    "harassment": "Harassment or hate",
    "sexual": "Sexual content without consent",
    "under_18": "Appears under 18",
    "threats": "Threats or violence",
    "other": "Other",
}


def summary_text(counts: alerts.OpenCounts) -> str:
    urgent = f" ({counts.urgent} urgent)" if counts.urgent else ""
    return SUMMARY_ALERT.format(open=counts.open, urgent=urgent)


class StaffAlerts:
    """Sends moderators and admins their alerts (D-071): urgent reports, queued when
    filed, and the daily summary of open reports, queued here once its hour has passed
    (an admin setting, India time). Runs in the worker.

    Alerts carry no chat content, report details, or profiles. With a public address,
    each has a button that opens the moderation Mini App, on the report for an urgent
    one. Failures are retried a few times; someone who blocked the bot is given up on.
    """

    def __init__(
        self,
        bot: Bot,
        sessions: async_sessionmaker[AsyncSession],
        settings: BotSettings,
        runtime_settings: RuntimeSettings,
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._base = settings.public_base_url
        self._runtime = runtime_settings

    def _button(self, report_id: int | None) -> InlineKeyboardMarkup | None:
        if self._base is None:
            return None
        url = f"{str(self._base).rstrip('/')}/moderation/"
        if report_id is not None:
            url += f"?report={report_id}"
        text = OPEN_PANEL_BUTTON if report_id is None else OPEN_REPORT_BUTTON
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]]
        )

    async def tick(self, now: datetime) -> int:
        """Queue today's summary if due, then send one batch; returns how many were sent."""
        async with self._sessions() as session:
            hour = await self._runtime.get(session, SettingKey.REPORT_SUMMARY_HOUR)
            await alerts.queue_summary(session, now, hour)
            await session.commit()
            claimed = await alerts.claim(session, now, ALERT_BATCH)
            # Counted only when a summary is going out, not on every tick.
            summaries = any(alert.kind == "summary" for alert in claimed)
            counts = await alerts.open_counts(session) if summaries else None
            await session.commit()
            sent = 0
            for alert in claimed:
                # Checked again right before sending, so a role removed or an account
                # restricted while the batch is being sent stops the rest of its alerts.
                still_staff = await alerts.still_staff(session, alert.id)
                await session.commit()
                if not still_staff:
                    await alerts.mark_sent(session, alert.id, now)
                    await session.commit()
                    continue
                if await self._send(alert, counts):
                    await alerts.mark_sent(session, alert.id, now)
                    await session.commit()
                    sent += 1
        return sent

    async def _send(self, alert: alerts.DueAlert, counts: alerts.OpenCounts | None) -> bool:
        """Send one alert; True if it is done with (sent, or never deliverable)."""
        if alert.kind == "urgent" and alert.filed_at is not None:
            text = URGENT_ALERT.format(
                reason=REASON_LABELS.get(str(alert.reason), str(alert.reason)),
                at=f"{alert.filed_at:%H:%M}",
            )
            markup = self._button(alert.report_id)
        elif counts is not None:
            text, markup = summary_text(counts), self._button(None)
        else:
            return False  # not reached: counts are loaded whenever a summary is claimed
        try:
            await self._bot.send_message(alert.telegram_user_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            logger.info("could not send a staff alert", extra=exception_summary(exc))
            return unreachable(exc)
        return True
