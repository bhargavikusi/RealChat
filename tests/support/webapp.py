"""Telegram Mini App launch data, signed as Telegram signs it, for sign-in tests."""

import hashlib
import hmac
import json
from datetime import datetime
from urllib.parse import urlencode

from tests.support.telegram import FAKE_TOKEN


def launch_data(
    telegram_user_id: int | None, signed_at: datetime, token: str = FAKE_TOKEN, **extra: str
) -> str:
    """The query string a Mini App receives, with Telegram's `hash` over its fields
    (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
    With no user id, there is no `user` field, as when launched from a keyboard button."""
    fields = {"query_id": "AAH-test", "auth_date": str(int(signed_at.timestamp())), **extra}
    if telegram_user_id is not None:
        user = {"id": telegram_user_id, "first_name": "Test"}
        fields["user"] = json.dumps(user, separators=(",", ":"))
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def authorization(telegram_user_id: int, signed_at: datetime, **kwargs: str) -> dict[str, str]:
    return {"Authorization": f"tma {launch_data(telegram_user_id, signed_at, **kwargs)}"}
