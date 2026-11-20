"""Splitting the terms into messages within Telegram's limit."""

import pytest

from backend.modules.users.terms import (
    DRAFT_LABEL,
    MESSAGE_LIMIT,
    PRIVACY_HEADING,
    TERMS_HEADING,
    TERMS_QUESTION,
    privacy_notice,
    split_message,
    terms_body,
    terms_of_use,
    terms_parts,
    terms_prompt_parts,
    terms_text,
)


def _length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_short_text_is_one_message() -> None:
    assert split_message("one\ntwo") == ["one\ntwo"]


def test_parts_end_at_line_breaks_and_rejoin_exactly() -> None:
    lines = [f"• bullet {n} " + "x" * 30 for n in range(10)]
    text = "\n".join(lines)
    parts = split_message(text, limit=100)
    assert all(_length(part) <= 100 for part in parts)
    assert "\n".join(parts) == text
    # Each part holds whole lines.
    assert all(line in lines for part in parts for line in part.split("\n"))


def test_emoji_count_as_telegram_counts_them() -> None:
    # Telegram counts UTF-16 code units: an emoji outside the basic plane is two.
    text = "😀" * 3 + "\n" + "😀" * 3
    assert split_message(text, limit=7) == ["😀" * 3, "😀" * 3]


def test_a_line_over_the_limit_is_refused() -> None:
    with pytest.raises(ValueError):
        split_message("x" * 11, limit=10)


@pytest.mark.parametrize("minimum_age", [13, 18, 21])
def test_the_terms_fit_in_messages_and_rejoin(minimum_age: int) -> None:
    for parts, whole in (
        (terms_parts(minimum_age), terms_body(minimum_age)),
        (terms_prompt_parts(minimum_age), terms_text(minimum_age)),
    ):
        assert all(_length(part) <= MESSAGE_LIMIT for part in parts)
        assert "\n".join(parts) == whole
    # The accept buttons go on the last part, which asks the question.
    assert terms_prompt_parts(minimum_age)[-1].endswith(TERMS_QUESTION)


def _bullets(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("• ")]


def test_the_bot_shows_the_terms_of_use_then_the_privacy_notice() -> None:
    body = terms_body(18)
    assert body == (
        f"{DRAFT_LABEL}\n\n{TERMS_HEADING}\n\n{terms_of_use(18)}\n\n"
        f"{PRIVACY_HEADING}\n\n{privacy_notice()}"
    )
    # Every bullet is in exactly one of the two parts (D-069).
    assert sorted(_bullets(body)) == sorted(_bullets(terms_of_use(18)) + _bullets(privacy_notice()))
    assert len(set(_bullets(body))) == len(_bullets(body)) == 17


def test_the_parts_hold_what_they_should() -> None:
    assert "• You must be at least 21 years old." in terms_of_use(21)
    assert any(
        b.startswith("• Coins are a virtual in-app currency.") for b in _bullets(terms_of_use(18))
    )
    assert any(
        b.startswith("• Messages pass through the RealChat bot.")
        for b in _bullets(privacy_notice())
    )
    assert any(b.startswith("• You can delete your account") for b in _bullets(privacy_notice()))
