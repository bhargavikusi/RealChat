"""Profile field rules (D-010 to D-013 and the agreed field limits).

Pure functions with no database access, so the bot, the Mini App API, and tests
all apply exactly the same rules. Each raises ProfileValidationError with a
message that is safe to show to the user and never echoes their input.
"""

import re
import unicodedata
from datetime import date
from enum import StrEnum

import pycountry

NICKNAME_MIN_LENGTH = 2
NICKNAME_MAX_LENGTH = 20
CITY_MAX_LENGTH = 60
BIO_MAX_LENGTH = 300
# Absolute bounds stored in the database. The eligibility minimum is a setting
# (D-009) applied on top of this and must not be lower than AGE_FLOOR.
AGE_FLOOR = 13
AGE_MAX = 99


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class Visibility(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"


class ProfileValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


# Links and contact handles are rejected in public text fields: they are the most
# common way spam and off-platform contact requests are injected into profiles.
_LINK = re.compile(
    r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.|\bt(?:elegram)?\.me/|\b[a-z0-9-]+\.(?:com|net|org|"
    r"io|me|ru|info|biz|xyz|app|dev|link|site|online|top|ly|gg|tv|co)\b)"
)
_HANDLE = re.compile(r"@[A-Za-z0-9_]{5,}")
_WHITESPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Normalize Unicode, drop control/format characters, and collapse whitespace."""
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if ch in "\n " or unicodedata.category(ch) not in {"Cc", "Cf"})
    return _WHITESPACE.sub(" ", text).strip()


def _reject_contacts(field: str, label: str, text: str) -> None:
    if _LINK.search(text):
        raise ProfileValidationError(field, f"{label} can't contain links.")
    if _HANDLE.search(text):
        raise ProfileValidationError(field, f"{label} can't contain @usernames.")


def validate_nickname(raw: str) -> str:
    nickname = _clean(raw)
    if not NICKNAME_MIN_LENGTH <= len(nickname) <= NICKNAME_MAX_LENGTH:
        raise ProfileValidationError(
            "nickname",
            f"Nickname must be {NICKNAME_MIN_LENGTH}-{NICKNAME_MAX_LENGTH} characters.",
        )
    _reject_contacts("nickname", "Nickname", nickname)
    return nickname


def validate_age(age: int, minimum_age: int) -> int:
    if minimum_age < AGE_FLOOR:
        # A configuration error, not user input: fail loudly.
        raise ValueError(f"minimum age setting must be at least {AGE_FLOOR}")
    if age < minimum_age:
        raise ProfileValidationError("age", f"You must be at least {minimum_age} to use RealChat.")
    if age > AGE_MAX:
        raise ProfileValidationError("age", "Please enter your real age.")
    return age


def parse_age(raw: str) -> int:
    text = raw.strip()
    if not text.isdigit() or len(text) > 3:
        raise ProfileValidationError("age", "Please send your age as a number, e.g. 24.")
    return int(text)


def validate_gender(raw: str) -> Gender:
    try:
        return Gender(raw)
    except ValueError:
        raise ProfileValidationError("gender", "Please choose one of the options.") from None


def validate_country_code(raw: str) -> str:
    code = raw.strip().upper()
    if len(code) != 2 or pycountry.countries.get(alpha_2=code) is None:
        raise ProfileValidationError("country", "That isn't a recognised country.")
    return code


def validate_city(raw: str) -> str:
    city = _clean(raw)
    if not 1 <= len(city) <= CITY_MAX_LENGTH:
        raise ProfileValidationError("city", f"City must be 1-{CITY_MAX_LENGTH} characters.")
    _reject_contacts("city", "City", city)
    return city


def validate_bio(raw: str) -> str:
    bio = _clean(raw)
    if not 1 <= len(bio) <= BIO_MAX_LENGTH:
        raise ProfileValidationError("bio", f"Bio must be 1-{BIO_MAX_LENGTH} characters.")
    _reject_contacts("bio", "Bio", bio)
    return bio


def current_age(age_at_entry: int, entered_on: date, today: date) -> int:
    """Estimated current age (D-010): the entered age plus full years since entry."""
    years = today.year - entered_on.year
    if (today.month, today.day) < (entered_on.month, entered_on.day):
        years -= 1
    return age_at_entry + max(years, 0)
