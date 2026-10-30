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
