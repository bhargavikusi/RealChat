"""Splitting the terms into messages within Telegram's limit."""

import pytest

from backend.modules.users.terms import (
    MESSAGE_LIMIT,
    TERMS_QUESTION,
    split_message,
    terms_body,
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
