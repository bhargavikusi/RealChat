"""What may be relayed between anonymous chat partners (D-042).

Contact cards and locations are never relayed, and text or captions containing a
link, a phone number, or an @username are refused, so neither partner reveals how to
reach them by accident. The checks run on a normalized copy of the text; the message
itself is relayed unchanged.

The phone-number check is a heuristic: seven or more digits in a number-like run,
optionally with "+", and with spaces, dots, dashes, or brackets between them (spaces
may surround the punctuation, as in "555 - 123 - 4567"). It can also catch long lists
of numbers, which is accepted.

Hidden links in formatting (a word that links to a URL, a mention of a user) and link
buttons are checked by the bot layer, which sees Telegram's message structure.
"""

import re
from enum import StrEnum

from backend.modules.users.validation import contains_handle, contains_link, normalize_for_checks

_PHONE = re.compile(r"\+?\d(?:\s*[().-]{0,2}\s*\d){6,}")


class Blocked(StrEnum):
    CONTACT = "contact"
    LOCATION = "location"
    LINK = "link"
    PHONE = "phone"
    USERNAME = "username"


def blocked_text(text: str) -> Blocked | None:
    """Why `text` (a message or caption) may not be relayed, or None if it may."""
    checked = normalize_for_checks(text)
    if contains_link(checked):
        return Blocked.LINK
    if _PHONE.search(checked):
        return Blocked.PHONE
    if contains_handle(checked):
        return Blocked.USERNAME
    return None
