import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from backend.core.logging import REDACTED, JsonFormatter, redact_text

# Obviously fake. It has the bot-token shape the redactor looks for (digits, colon,
# 30+ token characters) but not the "AA" prefix of real tokens, so secret scanners
# do not mistake it for a leaked credential.
BOT_TOKEN = "123456789:TEST-fake-token-for-redaction-check"
DB_URL = "postgresql+asyncpg://realchat:db-pass-8f3k2@localhost:5432/realchat"


@pytest.fixture
def log() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.redaction")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, stream
    logger.handlers.clear()


def _last_entry(stream: io.StringIO) -> dict[str, Any]:
    entry: dict[str, Any] = json.loads(stream.getvalue().strip().splitlines()[-1])
    return entry


def test_redacts_url_credentials() -> None:
    assert redact_text(f"connecting to {DB_URL}") == (
        f"connecting to postgresql+asyncpg://{REDACTED}@localhost:5432/realchat"
    )


def test_redacts_bot_token() -> None:
    assert redact_text(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe") == (
        f"https://api.telegram.org/bot{REDACTED}/getMe"
    )


def test_leaves_ordinary_text_alone() -> None:
    text = "user 42 joined queue at 12:30:00 https://example.com/path"
    assert redact_text(text) == text


def test_output_is_structured_json(log: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = log
    logger.info("match created", extra={"conversation_id": 7})

    entry = _last_entry(stream)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.redaction"
    assert entry["message"] == "match created"
    assert entry["conversation_id"] == 7
    assert entry["ts"].endswith("+00:00")


def test_secrets_never_reach_output(log: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = log
    logger.warning(
        "startup with %s",
        DB_URL,
        extra={
            "bot_token": BOT_TOKEN,
            "headers": {"Authorization": "Bearer abc.def", "Accept": "json"},
            "note": f"token is {BOT_TOKEN}",
        },
    )

    raw = stream.getvalue()
    assert BOT_TOKEN not in raw
    assert "db-pass-8f3k2" not in raw
    assert "abc.def" not in raw

    entry = _last_entry(stream)
    assert entry["bot_token"] == REDACTED
    assert entry["headers"] == {"Authorization": REDACTED, "Accept": "json"}


def test_exception_text_is_redacted(log: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = log
    try:
        raise ConnectionError(f"could not reach {DB_URL}")
    except ConnectionError:
        logger.exception("database unavailable")

    entry = _last_entry(stream)
    assert "ConnectionError" in entry["exception"]
    assert "db-pass-8f3k2" not in stream.getvalue()
