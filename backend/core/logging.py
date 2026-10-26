"""Structured JSON logging with redaction of secrets.

Redaction is a safety net, not permission to log sensitive data: callers must still
avoid logging message content, tokens, payment details, and precise locations.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

# Any extra field whose name contains one of these fragments is replaced entirely.
SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "cookie",
    "database_url",
    "redis_url",
    "dsn",
    "init_data",
)

# user:password@ inside a URL such as postgresql://user:password@host/db
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
# Telegram bot tokens look like 123456789:AAH... (numeric id, colon, long secret).
# No \b: in API URLs the token follows "bot" directly (/bot123456789:AAH...).
_BOT_TOKEN = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")

_STANDARD_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def redact_text(text: str) -> str:
    text = _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", text)
    return _BOT_TOKEN.sub(REDACTED, text)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(key, v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = redact_value(key, value)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=lambda obj: redact_text(str(obj)))


def configure_logging(level: str) -> None:
    """Send JSON logs to stdout, replacing any previously configured root handlers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
