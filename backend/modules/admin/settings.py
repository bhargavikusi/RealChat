"""Runtime settings that admins can change without a restart (D-035).

Each setting is a whole number with a default and an allowed range. Only values
an admin has changed are stored (`app_settings`); anything else uses the default.
A stored value outside the allowed range (written directly to the database, or left
over from an older range) is ignored in favour of the default.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.admin.models import AppSetting

logger = logging.getLogger(__name__)


class SettingKey(StrEnum):
    MATCH_REPEAT_WINDOW_SECONDS = "match_repeat_window_seconds"
    MATCH_SEARCH_TIMEOUT_SECONDS = "match_search_timeout_seconds"
    CHAT_IDLE_NOTICE_SECONDS = "chat_idle_notice_seconds"
    CHAT_IDLE_GRACE_SECONDS = "chat_idle_grace_seconds"
    CHAT_RETENTION_DAYS = "chat_retention_days"
    REPORTS_PER_DAY = "reports_per_day"
    PRIVATE_MODE_HOURS = "private_mode_hours"
    VIEW_ONCE_SECONDS = "view_once_seconds"
    PRIVATE_ENDED_ASK_SECONDS = "private_ended_ask_seconds"
    WELCOME_BONUS_COINS = "welcome_bonus_coins"
    INVITE_REWARD_COINS = "invite_reward_coins"
    INVITE_REWARDS_PER_DAY = "invite_rewards_per_day"
    DAILY_REWARD_ON = "daily_reward_on"
    DAILY_REWARD_COINS = "daily_reward_coins"
    PAID_MATCH_COINS = "paid_match_coins"
    PAST_CHATS_DAYS = "past_chats_days"
    VISITS_DAYS = "visits_days"
    USERID_MISSES_PER_HOUR = "userid_misses_per_hour"
    LIKE_NOTICE_GAP_HOURS = "like_notice_gap_hours"
    DIRECT_MESSAGE_COINS = "direct_message_coins"
    CHAT_REQUEST_COINS = "chat_request_coins"
    CHAT_REQUEST_ACTIVE_MINUTES = "chat_request_active_minutes"
    CHAT_REQUEST_EXPIRY_MINUTES = "chat_request_expiry_minutes"
    CHAT_REQUEST_SWITCH_SECONDS = "chat_request_switch_seconds"
    ACTIVITY_ALERT_COINS = "activity_alert_coins"
    ACTIVITY_ALERT_DAYS = "activity_alert_days"
    REMINDER_LOCAL_HOUR = "reminder_local_hour"
    REPORT_SUMMARY_HOUR = "report_summary_hour"
    PACK_1_COINS = "pack_1_coins"
    PACK_1_STARS = "pack_1_stars"
    PACK_2_COINS = "pack_2_coins"
    PACK_2_STARS = "pack_2_stars"
    PACK_3_COINS = "pack_3_coins"
    PACK_3_STARS = "pack_3_stars"
    PACK_4_COINS = "pack_4_coins"
    PACK_4_STARS = "pack_4_stars"
    VIP_STARS = "vip_stars"
    VIP_DAYS = "vip_days"
    VIP_COINS = "vip_coins"


@dataclass(frozen=True)
class SettingSpec:
    key: SettingKey
    label: str
    unit: str
    default: int
    minimum: int
    maximum: int
    note: str = ""

    def allows(self, value: int) -> bool:
        return self.minimum <= value <= self.maximum


# Coin packs sold for Telegram Stars (D-061): (coins, Stars) for packs 1 to 4.
PACK_DEFAULTS = ((150, 80), (350, 150), (800, 290), (1_900, 580))


def _pack_specs(number: int, coins: int, stars: int) -> tuple[SettingSpec, SettingSpec]:
    return (
        SettingSpec(
            SettingKey(f"pack_{number}_coins"),
            f"Coin pack {number}: coins",
            "coins",
            default=coins,
            minimum=0,
            maximum=100_000,
            note="0 hides the pack",
        ),
        SettingSpec(
            SettingKey(f"pack_{number}_stars"),
            f"Coin pack {number}: price",
            "Stars",
            default=stars,
            minimum=1,
            maximum=10_000,
        ),
    )


SPECS: dict[SettingKey, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            SettingKey.MATCH_REPEAT_WINDOW_SECONDS,
            "Repeat-match avoidance",
            "seconds",
            default=300,
            minimum=0,
            maximum=86_400,
            note="0 turns it off",
        ),
        SettingSpec(
            SettingKey.MATCH_SEARCH_TIMEOUT_SECONDS,
            "Search time limit",
            "seconds",
            default=120,
            minimum=30,
            maximum=1_800,
        ),
        SettingSpec(
            SettingKey.CHAT_IDLE_NOTICE_SECONDS,
            "Idle notice after",
            "seconds",
            default=300,
            minimum=60,
            maximum=86_400,
        ),
        SettingSpec(
            SettingKey.CHAT_IDLE_GRACE_SECONDS,
            "Idle chat ends after notice",
            "seconds",
            default=60,
            minimum=10,
            maximum=3_600,
        ),
        SettingSpec(
            SettingKey.CHAT_RETENTION_DAYS,
            "Chat message retention",
            "days",
            default=7,
            minimum=1,
            maximum=365,
        ),
        SettingSpec(
            SettingKey.REPORTS_PER_DAY,
            "Reports per user per 24 hours",
            "reports",
            default=10,
            minimum=1,
            maximum=100,
        ),
        SettingSpec(
            SettingKey.PRIVATE_MODE_HOURS,
            "Private mode time limit",
            "hours",
            default=24,
            minimum=1,
            # Telegram lets bots delete messages for 48 hours; this leaves time to retry.
            maximum=46,
        ),
        SettingSpec(
            SettingKey.VIEW_ONCE_SECONDS,
            "View-once timer",
            "seconds",
            default=20,
            minimum=10,
            maximum=300,
            note="added to the length of videos and voice messages",
        ),
        SettingSpec(
            SettingKey.PRIVATE_ENDED_ASK_SECONDS,
            "Ask before sending media after Private mode ends",
            "seconds",
            default=30,
            minimum=0,
            maximum=300,
            note="0 turns it off",
        ),
        SettingSpec(
            SettingKey.WELCOME_BONUS_COINS,
            "Welcome bonus",
            "coins",
            default=10,
            minimum=0,
            maximum=1_000,
            note="0 turns it off",
        ),
        SettingSpec(
            SettingKey.INVITE_REWARD_COINS,
            "Invite reward",
            "coins",
            default=10,
            minimum=0,
            maximum=1_000,
            note="0 turns it off",
        ),
        SettingSpec(
            SettingKey.INVITE_REWARDS_PER_DAY,
            "Invite rewards per inviter per day",
            "invites",
            default=10,
            minimum=0,
            maximum=1_000,
        ),
        SettingSpec(
            SettingKey.DAILY_REWARD_ON,
            "Daily reward (promotions)",
            "on/off",
            default=0,
            minimum=0,
            maximum=1,
            note="1 turns it on, 0 off",
        ),
        SettingSpec(
            SettingKey.DAILY_REWARD_COINS,
            "Daily reward amount",
            "coins",
            default=1,
            minimum=1,
            maximum=1_000,
        ),
        SettingSpec(
            SettingKey.PAID_MATCH_COINS,
            "Price of a chat with a chosen gender",
            "coins",
            default=2,
            minimum=1,
            maximum=1_000,
        ),
        SettingSpec(
            SettingKey.PAST_CHATS_DAYS,
            "How long past chat partners stay reachable",
            "days",
            default=10,
            minimum=1,
            maximum=30,
        ),
        SettingSpec(
            SettingKey.VISITS_DAYS,
            "How long profile visits are kept",
            "days",
            default=30,
            minimum=1,
            maximum=365,
        ),
        SettingSpec(
            SettingKey.USERID_MISSES_PER_HOUR,
            "Wrong RealChat ID lookups per user per hour",
            "lookups",
            default=10,
            minimum=1,
            maximum=100,
        ),
        SettingSpec(
            SettingKey.LIKE_NOTICE_GAP_HOURS,
            "Time before a pair's like notice can repeat",
            "hours",
            default=1,
            minimum=1,
            maximum=720,
        ),
        SettingSpec(
            SettingKey.DIRECT_MESSAGE_COINS,
            "Price of a direct message",
            "coins",
            default=1,
            minimum=1,
            maximum=100,
        ),
        SettingSpec(
            SettingKey.CHAT_REQUEST_COINS,
            "Price of a chat request",
            "coins",
            default=2,
            minimum=1,
            maximum=100,
        ),
        SettingSpec(
            SettingKey.CHAT_REQUEST_ACTIVE_MINUTES,
            "Chat requests: recipient active within",
            "minutes",
            default=10,
            minimum=1,
            maximum=60,
        ),
        SettingSpec(
            SettingKey.CHAT_REQUEST_EXPIRY_MINUTES,
            "Chat requests: time to accept",
            "minutes",
            default=60,
            minimum=5,
            maximum=1_440,
        ),
        SettingSpec(
            SettingKey.CHAT_REQUEST_SWITCH_SECONDS,
            "Chat requests: time for a busy sender to switch",
            "seconds",
            default=90,
            minimum=30,
            maximum=600,
        ),
        SettingSpec(
            SettingKey.ACTIVITY_ALERT_COINS,
            "Price of an activity alert",
            "coins",
            default=1,
            minimum=1,
            maximum=100,
        ),
        SettingSpec(
            SettingKey.ACTIVITY_ALERT_DAYS,
            "Activity alerts: days before a refund",
            "days",
            default=7,
            minimum=1,
            maximum=30,
        ),
        SettingSpec(
            SettingKey.REMINDER_LOCAL_HOUR,
            "Daily reward reminder: local hour without a claim to go by",
            "o'clock",
            default=18,
            minimum=0,
            maximum=23,
        ),
        # D-071: when moderators and admins get the day's summary of open reports.
        SettingSpec(
            SettingKey.REPORT_SUMMARY_HOUR,
            "Daily open-reports summary: hour",
            "o'clock",
            default=9,
            minimum=0,
            maximum=23,
            note="India time",
        ),
        *(
            spec
            for number, (coins, stars) in enumerate(PACK_DEFAULTS, start=1)
            for spec in _pack_specs(number, coins, stars)
        ),
        SettingSpec(
            SettingKey.VIP_STARS,
            "VIP pass: price",
            "Stars",
            default=780,
            minimum=1,
            maximum=10_000,
        ),
        SettingSpec(
            SettingKey.VIP_DAYS,
            "VIP pass: length",
            "days",
            default=180,
            minimum=1,
            maximum=366,
            note="also the most VIP time left at which another pass can be bought",
        ),
        SettingSpec(
            SettingKey.VIP_COINS,
            "VIP pass: coins included",
            "coins",
            default=3_750,
            minimum=0,
            maximum=100_000,
        ),
    )
}


async def load_values(session: AsyncSession) -> dict[SettingKey, int]:
    """Every setting's current value, read from the database."""
    # Plain columns, not ORM objects, so values already loaded in this session are
    # never served from its identity map.
    rows = await session.execute(select(AppSetting.key, AppSetting.value))
    stored = {key: value for key, value in rows}
    values: dict[SettingKey, int] = {}
    for key, spec in SPECS.items():
        value = stored.get(key.value)
        if value is not None and not spec.allows(value):
            logger.warning("stored setting out of range, using default", extra={"key": key.value})
            value = None
        values[key] = spec.default if value is None else value
    return values


class RuntimeSettings:
    """Reads settings with a short in-process cache.

    A change made by an admin is seen by every process within `ttl_seconds`, and at
    once by the process that made it (which calls `invalidate`).
    """

    def __init__(
        self, ttl_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._values: dict[SettingKey, int] | None = None
        self._loaded_at = 0.0
        # Bumped by invalidate(); a load that started before it is not cached.
        self._generation = 0

    async def get(self, session: AsyncSession, key: SettingKey) -> int:
        if self._values is not None and self._clock() - self._loaded_at < self._ttl:
            return self._values[key]
        generation = self._generation
        values = await load_values(session)
        if generation == self._generation:
            self._values, self._loaded_at = values, self._clock()
        return values[key]

    def invalidate(self) -> None:
        self._generation += 1
        self._values = None
