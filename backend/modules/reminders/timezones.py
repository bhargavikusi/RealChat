"""A country's main time zone, for reminders timed to local hours (D-060).

Read from `zone.tab` in the `tzdata` package, which lists every zone with its country.
A country with several zones takes the one named in `MAIN_ZONES` (its most populous
area), otherwise the first listed. This is an approximation for multi-zone countries,
which is acceptable for picking a reminder hour.
"""

from functools import cache
from importlib.resources import files
from zoneinfo import ZoneInfo

UTC_ZONE = "UTC"

# Countries spanning several zones, with the zone most of their people live in.
MAIN_ZONES = {
    "AR": "America/Argentina/Buenos_Aires",
    "AU": "Australia/Sydney",
    "BR": "America/Sao_Paulo",
    "CA": "America/Toronto",
    "CD": "Africa/Kinshasa",
    "CL": "America/Santiago",
    "CN": "Asia/Shanghai",
    "EC": "America/Guayaquil",
    "ES": "Europe/Madrid",
    "ID": "Asia/Jakarta",
    "KZ": "Asia/Almaty",
    "MN": "Asia/Ulaanbaatar",
    "MX": "America/Mexico_City",
    "MY": "Asia/Kuala_Lumpur",
    "NZ": "Pacific/Auckland",
    "PT": "Europe/Lisbon",
    "RU": "Europe/Moscow",
    "UA": "Europe/Kyiv",
    "US": "America/New_York",
}


@cache
def _zones() -> dict[str, str]:
    """Country code -> the first zone `zone.tab` lists for it."""
    table = files("tzdata").joinpath("zoneinfo", "zone.tab").read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for line in table.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        found.setdefault(fields[0], fields[2])
    return found


def country_zone(country_code: str | None) -> ZoneInfo:
    """The main time zone of a country (ISO 3166 alpha-2), or UTC if unknown."""
    code = (country_code or "").upper()
    return ZoneInfo(MAIN_ZONES.get(code) or _zones().get(code) or UTC_ZONE)
