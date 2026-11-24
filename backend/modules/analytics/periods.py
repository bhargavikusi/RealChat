"""The periods and India-day buckets shared by the admin dashboards (D-072)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Date, Integer, and_, cast, func, literal
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

ZONE = ZoneInfo("Asia/Kolkata")


class Period(StrEnum):
    WEEK = "7d"
    MONTH = "30d"
    QUARTER = "90d"
    YEAR = "12m"


# Days covered, and the bucket size in days (a year is shown by week).
SPANS = {Period.WEEK: (7, 1), Period.MONTH: (30, 1), Period.QUARTER: (90, 1), Period.YEAR: (364, 7)}


@dataclass(frozen=True)
class Window:
    """A period ending today (India): its first day, bucket size, and bucket starts."""

    today: date
    first: date
    size: int
    starts: list[date]

    @classmethod
    def of(cls, period: Period, now: datetime) -> "Window":
        today = now.astimezone(ZONE).date()
        days, size = SPANS[period]
        first = today - timedelta(days=days - 1)
        return cls(today, first, size, [first + timedelta(days=n) for n in range(0, days, size)])

    def contains(self, day: ColumnElement[date]) -> ColumnElement[bool]:
        """Whether an India day is in the window, from its first day to today. Every
        period figure uses it: without the lower bound, PostgreSQL's integer division
        (which rounds toward zero) would put days just before the window into the
        first week; the upper bound keeps anything dated after today out."""
        return and_(day >= self.first, day <= self.today)

    def bucket(self, day: ColumnElement[date]) -> ColumnElement[date]:
        """The start of the bucket an India day falls in. In PostgreSQL, a date minus a
        date is a number of days, and a date plus a number of days is a date."""
        start = literal(self.first, Date)
        return cast(start + cast(day - start, Integer) // self.size * self.size, Date)


def india_day(
    column: InstrumentedAttribute[datetime]
    | InstrumentedAttribute[datetime | None]
    | ColumnElement[Any],
) -> ColumnElement[date]:
    """A timestamp column as its India calendar date."""
    return cast(func.timezone("Asia/Kolkata", column), Date)
