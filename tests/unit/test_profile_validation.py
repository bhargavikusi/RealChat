from datetime import date

import pytest

from backend.modules.users import validation as v
from backend.modules.users.validation import Gender, ProfileValidationError


@pytest.mark.parametrize("raw", ["Al", "Night Owl", "Привет", "猫猫", "  spaced   out  ", "a" * 20])
def test_valid_nicknames(raw: str) -> None:
    assert v.validate_nickname(raw) == " ".join(raw.split())


@pytest.mark.parametrize(
    "raw",
    [
        "A",  # too short
        "a" * 21,  # too long
        "   ",  # only whitespace
        "visit www.spam",
        "see t.me/somechannel",
        "http://x",
        "shop.com",
        "add @someone1",
    ],
)
def test_invalid_nicknames(raw: str) -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        v.validate_nickname(raw)
    assert excinfo.value.field == "nickname"
    # Messages never echo the user's input back.
    if raw.strip():
        assert raw.strip() not in excinfo.value.message


def test_control_and_invisible_characters_are_removed() -> None:
    assert v.validate_nickname("Ni​ck\x00") == "Nick"


@pytest.mark.parametrize("raw,expected", [("18", 18), (" 42 ", 42), ("99", 99), ("١٨", 18)])
def test_parse_age(raw: str, expected: int) -> None:
    assert v.parse_age(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "-5", "4.5", "1000", "eighteen", "²", "1²"])
def test_parse_age_rejects_non_numbers(raw: str) -> None:
    with pytest.raises(ProfileValidationError):
        v.parse_age(raw)


def test_age_must_meet_configured_minimum() -> None:
    assert v.validate_age(18, minimum_age=18) == 18
    with pytest.raises(ProfileValidationError, match="at least 18"):
        v.validate_age(17, minimum_age=18)
    with pytest.raises(ProfileValidationError):
        v.validate_age(100, minimum_age=18)


@pytest.mark.parametrize("minimum_age", [12, 100])
def test_minimum_age_setting_must_be_within_bounds(minimum_age: int) -> None:
    with pytest.raises(ValueError, match="between 13 and 99"):
        v.validate_age(30, minimum_age=minimum_age)


def test_gender_options() -> None:
    assert [g.value for g in Gender] == ["male", "female", "other"]
    assert v.validate_gender("other") is Gender.OTHER
    with pytest.raises(ProfileValidationError):
        v.validate_gender("unknown")


@pytest.mark.parametrize("raw,expected", [("in", "IN"), (" de ", "DE"), ("US", "US")])
def test_valid_country_codes(raw: str, expected: str) -> None:
    assert v.validate_country_code(raw) == expected


@pytest.mark.parametrize("raw", ["ZZ", "USA", "", "1A"])
def test_invalid_country_codes(raw: str) -> None:
    with pytest.raises(ProfileValidationError):
        v.validate_country_code(raw)


def test_city_and_bio_limits() -> None:
    assert v.validate_city("São Paulo") == "São Paulo"
    assert v.validate_bio("I like music and node.js") == "I like music and node.js"
    with pytest.raises(ProfileValidationError):
        v.validate_city("x" * 61)
    with pytest.raises(ProfileValidationError):
        v.validate_bio("x" * 301)
    with pytest.raises(ProfileValidationError, match="links"):
        v.validate_bio("follow me at insta.com/me")


@pytest.mark.parametrize(
    "entered_on,today,expected",
    [
        (date(2026, 3, 10), date(2026, 3, 10), 20),
        (date(2026, 3, 10), date(2027, 3, 9), 20),  # day before the anniversary
        (date(2026, 3, 10), date(2027, 3, 10), 21),  # on the anniversary
        (date(2026, 3, 10), date(2031, 12, 31), 25),
        (date(2024, 2, 29), date(2025, 2, 28), 20),  # leap-day entry
        (date(2024, 2, 29), date(2025, 3, 1), 21),
        (date(2026, 3, 10), date(2025, 1, 1), 20),  # clock before entry never lowers age
    ],
)
def test_current_age_advances_once_per_full_year(
    entered_on: date, today: date, expected: int
) -> None:
    assert v.current_age(20, entered_on, today) == expected


@pytest.mark.parametrize("text", ["example.de", "visit shop.in today", "my.site", "SHOP.CO.UK"])
def test_bare_domains_with_country_endings_are_rejected(text: str) -> None:
    with pytest.raises(ProfileValidationError, match="links"):
        v.validate_bio(text)


@pytest.mark.parametrize(
    "text", ["node.js fan", "e.g. music", "St. Louis", "Mr.Smith", "3.5 stars"]
)
def test_ordinary_text_with_dots_is_allowed(text: str) -> None:
    assert v.validate_bio(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "visit ex\u0430mple.com",  # a Cyrillic small a
        "example.\u034fcom",  # a combining grapheme joiner
        "\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.com",  # full-width letters
    ],
)
def test_disguised_links_are_rejected_in_profiles(text: str) -> None:
    with pytest.raises(ProfileValidationError, match="links"):
        v.validate_bio(text)


def test_disguised_handles_are_rejected_in_profiles() -> None:
    with pytest.raises(ProfileValidationError, match="@usernames"):
        v.validate_bio("find me at @\u034fnight_owl_99")
