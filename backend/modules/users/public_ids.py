"""RealChat IDs: the public, random ID on every profile card (D-054).

Seven characters from lowercase letters and digits, leaving out look-alikes (0 and o,
1, l, and i), so an ID is easy to read and type and fits a Telegram command such as
/userid_k7m3qxa. IDs are drawn at random, so they carry nothing about the account and
cannot be used to count users. Uniqueness is enforced by the database.
"""

import re
import secrets

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
LENGTH = 7
_FORMAT = re.compile(f"[{ALPHABET}]{{{LENGTH}}}")
# The same rule as a PostgreSQL check constraint on `profiles.public_id`.
SQL_FORMAT = f"public_id ~ '^[{ALPHABET}]{{{LENGTH}}}$'"


def new_public_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def normalize(raw: str) -> str | None:
    """The ID as stored, ignoring letter case and surrounding spaces; None if `raw`
    cannot be an ID. Only ASCII is accepted, so a look-alike such as the Kelvin sign,
    which lowercases to "k", is not taken for a letter."""
    candidate = raw.strip()
    if not candidate.isascii():
        return None
    candidate = candidate.lower()
    return candidate if _FORMAT.fullmatch(candidate) else None
