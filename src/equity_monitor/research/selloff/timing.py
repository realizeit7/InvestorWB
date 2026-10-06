"""Event timing under the sr-0.x convention (docs/selloff/PROTOCOL.md §8). Pure functions; no intraday precision is
ever invented from a date-only source.

- latest possible public time  = EDGAR acceptance (exact); without it, the end of the filing date (date-only)
- earliest possible public time = the earliest of: acceptance, a release dateline (00:00 New York that date) and the
  8-K's "date of earliest event reported" (00:00 New York) — date-only bounds widen the window, never narrow it
- pre_session         = last session that CLOSES before the earliest possible public time
- measurement_session = first session that CLOSES after the latest possible public time (the decline is observable)
- decision cutoff     = measurement close + data-availability lag (the signal needs the completed day)
- entry_session       = first session that OPENS after the cutoff
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from ...data import calendar as cal
from ...util import NY, ensure_utc

DEFAULT_LAG_MINUTES = 120


@dataclass
class EventTiming:
    public_earliest: datetime
    public_latest: datetime
    precision: str                       # EXACT | DATE_ONLY
    basis: str
    pre_session: date
    measurement_session: date
    decision_cutoff: datetime
    entry_session: date
    notes: list[str] = field(default_factory=list)


def start_of_day(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=NY)


def end_of_day(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=NY)


def last_close_before(t: datetime) -> date:
    s = cal.session_on_or_before(cal.ny_date(t))
    while cal.session_close_utc(s) >= ensure_utc(t):
        s = cal.previous_session(s)
    return s


def first_close_after(t: datetime) -> date:
    s = cal.session_on_or_after(cal.ny_date(t))
    while cal.session_close_utc(s) <= ensure_utc(t):
        s = cal.next_session(s)
    return s


def first_open_after(t: datetime) -> date:
    s = cal.session_on_or_after(cal.ny_date(t))
    while cal.session_open_utc(s) <= ensure_utc(t):
        s = cal.next_session(s)
    return s


def event_timing(*, accepted: datetime | None, filing_date: str | None, dateline: str | None = None,
                 report_date: str | None = None, lag_minutes: int = DEFAULT_LAG_MINUTES) -> EventTiming:
    notes: list[str] = []
    if accepted is not None:
        latest, latest_basis = ensure_utc(accepted), "EDGAR acceptance (exact)"
    elif filing_date:
        latest, latest_basis = ensure_utc(end_of_day(date.fromisoformat(filing_date))), "filing date, end of day (date-only)"
        notes.append("no acceptance time: latest public time is the end of the filing date")
    else:
        raise ValueError("an event needs an acceptance time or a filing date")
    bounds = [(latest, latest_basis)]
    for d, label in ((dateline, "release dateline"), (report_date, "8-K date of earliest event reported")):
        if d:
            t = ensure_utc(start_of_day(date.fromisoformat(d)))
            if t <= latest:
                bounds.append((t, f"{label} {d} (date-only, 00:00 New York)"))
            else:
                notes.append(f"{label} {d} is after the filing: ignored as a bound")
    earliest, basis = min(bounds, key=lambda b: b[0])
    precision = "EXACT" if accepted is not None and earliest == latest else "DATE_ONLY"
    pre = last_close_before(earliest)
    meas = first_close_after(latest)
    cutoff = ensure_utc(cal.session_close_utc(meas)) + timedelta(minutes=lag_minutes)
    entry = first_open_after(cutoff)
    if cal.sessions_elapsed(pre, meas) > 1:
        notes.append(f"decline window spans {cal.sessions_elapsed(pre, meas)} sessions (date-only bounds)")
    return EventTiming(earliest, latest, precision, f"earliest: {basis}; latest: {latest_basis}", pre, meas, cutoff,
                       entry, notes)
