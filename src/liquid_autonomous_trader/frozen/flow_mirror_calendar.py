"""NYSE session helpers for Flow Mirror risk-epoch resets."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
CALENDAR_VALID_FROM = date(2000, 1, 1)
CALENDAR_VALID_THROUGH = date(2026, 12, 31)

EXCEPTIONAL_CLOSURES = {
    date(2001, 9, 11),
    date(2001, 9, 12),
    date(2001, 9, 13),
    date(2001, 9, 14),
    date(2004, 6, 11),
    date(2007, 1, 2),
    date(2012, 10, 29),
    date(2012, 10, 30),
    date(2018, 12, 5),
    date(2025, 1, 9),
}


def flow_mirror_risk_epoch(value: datetime) -> date:
    """Return the XNYS session whose risk budget remains in force."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("risk-epoch timestamp must be timezone-aware")
    local = value.astimezone(NEW_YORK)
    candidate = local.date()
    if is_nyse_session(candidate) and local.time() >= REGULAR_OPEN:
        return candidate
    return previous_nyse_session(candidate)


def next_risk_epoch_open(epoch: date) -> datetime:
    candidate = epoch + timedelta(days=1)
    while not is_nyse_session(candidate):
        candidate += timedelta(days=1)
    return datetime.combine(candidate, REGULAR_OPEN, tzinfo=NEW_YORK)


def previous_nyse_session(value: date) -> date:
    candidate = value - timedelta(days=1)
    while not is_nyse_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def is_nyse_session(value: date) -> bool:
    if not CALENDAR_VALID_FROM <= value <= CALENDAR_VALID_THROUGH:
        raise ValueError(
            "XNYS session calendar is unavailable outside its reviewed coverage window"
        )
    if value.weekday() >= 5 or value in EXCEPTIONAL_CLOSURES:
        return False
    holidays = (
        nyse_holidays(value.year - 1) | nyse_holidays(value.year) | nyse_holidays(value.year + 1)
    )
    return value not in holidays


@cache
def nyse_holidays(year: int) -> frozenset[date]:
    holidays: set[date] = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    return frozenset(holidays)


def _observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)
