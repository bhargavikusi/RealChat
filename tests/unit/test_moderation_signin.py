"""Checking the moderation Mini App's Telegram launch data (D-071)."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode

import pytest

from apps.api.moderation import CLOCK_SKEW, MAX_AGE, Unauthorized, verify_launch_data
from tests.support.telegram import FAKE_TOKEN
from tests.support.webapp import launch_data

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
USER = 7101
OTHER_TOKEN = "987654321:TEST-another-bots-token-for-tests"


def _header(data: str) -> str:
    return f"tma {data}"


def _changed(data: str, **fields: str) -> str:
    """The launch data with fields replaced, keeping the original hash."""
    parsed = dict(parse_qsl(data))
    parsed.update(fields)
    return urlencode(parsed)


@pytest.mark.parametrize("age", [timedelta(0), MAX_AGE, -CLOCK_SKEW])
def test_fresh_signed_data_gives_the_user(age: timedelta) -> None:
    header = _header(launch_data(USER, NOW - age))
    assert verify_launch_data(FAKE_TOKEN, header, NOW) == USER


@pytest.mark.parametrize(
    "age", [MAX_AGE + timedelta(seconds=1), -CLOCK_SKEW - timedelta(seconds=1)]
)
def test_stale_or_future_data_is_refused(age: timedelta) -> None:
    with pytest.raises(Unauthorized):
        verify_launch_data(FAKE_TOKEN, _header(launch_data(USER, NOW - age)), NOW)


def test_data_signed_for_another_bot_is_refused() -> None:
    header = _header(launch_data(USER, NOW, token=OTHER_TOKEN))
    with pytest.raises(Unauthorized):
        verify_launch_data(FAKE_TOKEN, header, NOW)


@pytest.mark.parametrize(
    "fields",
    [
        {"user": '{"id":1,"first_name":"Test"}'},  # someone else's id
        {"auth_date": str(int(NOW.timestamp()) + 3600)},  # a newer date
        {"hash": "0" * 64},
    ],
)
def test_changed_data_is_refused(fields: dict[str, str]) -> None:
    header = _header(_changed(launch_data(USER, NOW - timedelta(minutes=30)), **fields))
    with pytest.raises(Unauthorized):
        verify_launch_data(FAKE_TOKEN, header, NOW)


def test_data_without_a_hash_is_refused() -> None:
    parsed = dict(parse_qsl(launch_data(USER, NOW)))
    del parsed["hash"]
    with pytest.raises(Unauthorized):
        verify_launch_data(FAKE_TOKEN, _header(urlencode(parsed)), NOW)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer abc",
        "tma ",
        "tma not-a-query-string",
        f"TMA {launch_data(USER, NOW)}",
        launch_data(USER, NOW),  # no scheme
    ],
)
def test_malformed_headers_are_refused(header: str | None) -> None:
    with pytest.raises(Unauthorized):
        verify_launch_data(FAKE_TOKEN, header, NOW)


def test_signed_data_without_a_user_is_refused() -> None:
    # A valid signature, but no user (as when launched from a keyboard button).
    with pytest.raises(Unauthorized):
        verify_launch_data(FAKE_TOKEN, _header(launch_data(None, NOW)), NOW)
