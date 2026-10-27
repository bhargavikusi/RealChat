"""Match a typed country name to ISO 3166-1 countries for confirmation by the user."""

from dataclasses import dataclass

import pycountry

MAX_CANDIDATES = 3

# Common names and spellings that pycountry's search does not recognise.
_ALIASES = {
    "uk": "GB",
    "britain": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "usa": "US",
    "america": "US",
    "united states of america": "US",
    "uae": "AE",
    "emirates": "AE",
    "deutschland": "DE",
    "brasil": "BR",
    "espana": "ES",
    "españa": "ES",
    "italia": "IT",
    "nippon": "JP",
    "holland": "NL",
    "ivory coast": "CI",
    "cote d'ivoire": "CI",
    "turkey": "TR",
    "turkiye": "TR",
    "türkiye": "TR",
    "czech republic": "CZ",
    "south korea": "KR",
    "north korea": "KP",
    "bharat": "IN",
}


@dataclass(frozen=True)
class CountryMatch:
    code: str
    name: str


def _match(code: str) -> CountryMatch:
    country = pycountry.countries.get(alpha_2=code)
    if country is None:
        raise ValueError(f"unknown country code {code!r}")
    return CountryMatch(code=country.alpha_2, name=getattr(country, "common_name", country.name))


def country_name(code: str) -> str:
    """Display name for a stored ISO 3166-1 alpha-2 code."""
    return _match(code).name


def match_country(query: str) -> list[CountryMatch]:
    """Up to MAX_CANDIDATES countries matching the query, best first. Empty if none.

    An exact code, alias, or name returns a single match; otherwise a fuzzy search
    returns candidates for the user to choose from.
    """
    text = " ".join(query.split()).casefold()
    if not text or len(text) > 60:
        return []
    if text in _ALIASES:
        return [_match(_ALIASES[text])]
    if len(text) in (2, 3):
        exact = pycountry.countries.get(alpha_2=text.upper()) or pycountry.countries.get(
            alpha_3=text.upper()
        )
        if exact is not None:
            return [_match(exact.alpha_2)]
    for country in pycountry.countries:
        names = {
            country.name,
            getattr(country, "common_name", ""),
            getattr(country, "official_name", ""),
        }
        if text in {n.casefold() for n in names if n}:
            return [_match(country.alpha_2)]
    try:
        found = pycountry.countries.search_fuzzy(text)
    except LookupError:
        return []
    return [_match(country.alpha_2) for country in found[:MAX_CANDIDATES]]
