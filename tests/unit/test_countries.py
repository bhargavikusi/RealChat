import pytest

from backend.modules.users.countries import MAX_CANDIDATES, match_country


@pytest.mark.parametrize(
    "query,code",
    [
        ("India", "IN"),
        ("  india  ", "IN"),
        ("IN", "IN"),
        ("deu", "DE"),
        ("usa", "US"),
        ("UK", "GB"),
        ("England", "GB"),
        ("Deutschland", "DE"),
        ("Brasil", "BR"),
        ("Ivory Coast", "CI"),
        ("Russian Federation", "RU"),
    ],
)
def test_exact_names_codes_and_aliases_give_one_match(query: str, code: str) -> None:
    assert [m.code for m in match_country(query)] == [code]


def test_ambiguous_names_offer_choices() -> None:
    codes = {m.code for m in match_country("korea")}
    assert codes == {"KP", "KR"}


def test_fuzzy_candidates_are_capped() -> None:
    # "guin" is not an exact name and fuzzily matches more than MAX_CANDIDATES
    # countries (Guinea, Guinea-Bissau, Equatorial Guinea, Papua New Guinea).
    assert len(match_country("guin")) == MAX_CANDIDATES


@pytest.mark.parametrize("query", ["", "   ", "Atlantis", "x" * 61])
def test_no_match(query: str) -> None:
    assert match_country(query) == []
