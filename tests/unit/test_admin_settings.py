"""Runtime settings definitions and value parsing (D-035)."""

import pytest

from backend.modules.admin.service import AdminError, parse_setting_value
from backend.modules.admin.settings import SPECS, SettingKey


def test_every_setting_is_defined_with_a_default_inside_its_range() -> None:
    assert set(SPECS) == set(SettingKey)
    for spec in SPECS.values():
        assert spec.minimum <= spec.default <= spec.maximum


def test_approved_defaults() -> None:
    defaults = {key: spec.default for key, spec in SPECS.items()}
    assert defaults == {
        SettingKey.MATCH_REPEAT_WINDOW_SECONDS: 300,
        SettingKey.MATCH_SEARCH_TIMEOUT_SECONDS: 120,
        SettingKey.CHAT_IDLE_NOTICE_SECONDS: 300,
        SettingKey.CHAT_IDLE_GRACE_SECONDS: 60,
        SettingKey.CHAT_RETENTION_DAYS: 7,
        SettingKey.REPORTS_PER_DAY: 10,
    }


def test_repeat_avoidance_can_be_turned_off() -> None:
    assert parse_setting_value(SettingKey.MATCH_REPEAT_WINDOW_SECONDS, "0") == 0


@pytest.mark.parametrize(("raw", "value"), [("7", 7), (" 30 ", 30), ("365", 365), ("1", 1)])
def test_valid_values_are_parsed(raw: str, value: int) -> None:
    assert parse_setting_value(SettingKey.CHAT_RETENTION_DAYS, raw) == value


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "0",  # below the minimum of 1 day
        "366",
        "-1",
        "+7",
        "7.5",
        "seven",
        "7 days",
        "\u0667",  # ARABIC-INDIC DIGIT SEVEN: a digit, but not ASCII
        "9" * 5000,  # longer than int() accepts
    ],
)
def test_invalid_values_are_rejected_with_the_allowed_range(raw: str) -> None:
    with pytest.raises(AdminError) as excinfo:
        parse_setting_value(SettingKey.CHAT_RETENTION_DAYS, raw)
    assert excinfo.value.message == "Send a whole number from 1 to 365 (days)."
