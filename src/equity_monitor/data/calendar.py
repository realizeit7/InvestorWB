"""NYSE trading calendar (rule-based) with DST-aware session times.

Rules implemented (NYSE Rule 7.2 holidays + ad-hoc closures):
- New Year's Day (Sunday -> Monday; Saturday -> not observed), MLK Day, Washington's Birthday,
  Good Friday, Memorial Day, Juneteenth (from 2022), Independence Day, Labor Day, Thanksgiving,
  Christmas (Saturday -> Friday, Sunday -> Monday for the mid-year holidays).
- 13:00 ET early closes: July 3 (when July 4 falls Tue-Fri), the day after Thanksgiving,
  and December 24 (when it is a regular weekday session).
- Ad-hoc closures are listed explicitly in ``SPECIAL_CLOSURES``; new ones must be added by hand.

Limitation: unscheduled closures (weather, national mourning) are only known once announced.
The calendar is documented as covering 2000-2030; see DATA_COVERAGE.md.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

from ..util import NY, UTC, ensure_utc, ny_datetime

SPECIAL_CLOSURES: dict[date, str] = {
    date(2001, 9, 11): "September 11", date(2001, 9, 12): "September 11",
    date(2001, 9, 13): "September 11", date(2001, 9, 14): "September 11",
    date(2004, 6, 11): "Reagan mourning", date(2007, 1, 2): "Ford mourning",
    date(2012, 10, 29): "Hurricane Sandy", date(2012, 10, 30): "Hurricane Sandy",
    date(2018, 12, 5): "G.H.W. Bush mourning", date(2025, 1, 9): "Carter mourning",
}

REGULAR_CLOSE = (16, 0)
EARLY_CLOSE = (13, 0)
OPEN = (9, 30)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(year: int) -> dict[date, str]:
    h: dict[date, str] = {}
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        h[ny + timedelta(days=1)] = "New Year's Day (observed)"
    elif ny.weekday() != 5:
        h[ny] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[_easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    h[_observed(date(year, 12, 25))] = "Christmas Day"
    for d, name in SPECIAL_CLOSURES.items():
        if d.year == year:
            h[d] = name
    return h


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def is_early_close(d: date) -> bool:
    if not is_session(d):
        return False
    if d.month == 7 and d.day == 3 and date(d.year, 7, 4).weekday() in (1, 2, 3, 4):
        return True
    if d.month == 11 and d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return True
    if d.month == 12 and d.day == 24:
        return True
    return False


def session_open_utc(d: date) -> datetime:
    return ny_datetime(d, *OPEN)


def session_close_utc(d: date) -> datetime:
    return ny_datetime(d, *(EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE))


def next_session(d: date) -> date:
    d = d + timedelta(days=1)
    while not is_session(d):
        d += timedelta(days=1)
    return d


def previous_session(d: date) -> date:
    d = d - timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def session_on_or_after(d: date) -> date:
    return d if is_session(d) else next_session(d)


def session_on_or_before(d: date) -> date:
    return d if is_session(d) else previous_session(d)


def sessions_between(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def latest_completed_session(now: datetime, availability_lag_minutes: int = 120) -> date:
    """Latest session whose close + provider lag is at or before ``now``.

    The lag models when end-of-day data is reliably available from the provider.
    """
    now = ensure_utc(now)
    d = now.astimezone(NY).date()
    while True:
        if is_session(d) and session_close_utc(d) + timedelta(minutes=availability_lag_minutes) <= now:
            return d
        d -= timedelta(days=1)


def sessions_elapsed(after: date, upto: date) -> int:
    """Number of sessions strictly after ``after`` and up to and including ``upto``."""
    if upto <= after:
        return 0
    return len(sessions_between(after + timedelta(days=1), upto))


def ny_date(dt: datetime) -> date:
    return ensure_utc(dt).astimezone(NY).date()


__all__ = [
    "holidays", "is_session", "is_early_close", "session_open_utc", "session_close_utc",
    "next_session", "previous_session", "session_on_or_after", "session_on_or_before",
    "sessions_between", "latest_completed_session", "sessions_elapsed", "ny_date", "UTC",
]
