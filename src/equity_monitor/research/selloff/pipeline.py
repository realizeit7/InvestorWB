"""Reproducible feasibility pipeline after screening: events → point-in-time sources → financing records → candidate
statements → prices (identity guard) → eligibility → outcome COVERAGE audit. Every step is idempotent and timed
(``sr_effort``) so the report can state automated effort per event. No forward returns are computed."""

from __future__ import annotations

import time
from datetime import date

from ...app import App
from ...data import calendar as cal
from ...db.core import all_rows, insert, one
from ...util import new_id
from . import pricing, records
from .filings import Fetch, submissions
from .screening import build_events


class _Timer:
    def __init__(self, app: App, event_id: str | None, step: str):
        self.app, self.event_id, self.step = app, event_id, step

    def __enter__(self):
        self.t = time.monotonic()

    def __exit__(self, *exc):
        insert(self.app.conn, "sr_effort", {"id": new_id("sref"), "event_id": self.event_id, "step": self.step,
                                            "seconds": round(time.monotonic() - self.t, 3), "automated": 1,
                                            "note": "wall-clock incl. rate-limited network" if not exc[0] else f"failed: {exc[1]}"[:200],
                                            "created_at": self.app.now_iso()})


def events(app: App) -> list[dict]:
    return [dict(r) for r in all_rows(app.conn, "SELECT * FROM sr_event ORDER BY public_earliest")]


def run(app: App, *, fetch: Fetch, history: pricing.History, fetch_facts=None, today: date | None = None,
        prices: bool = True) -> dict:
    today = today or cal.latest_completed_session(app.now())
    with _Timer(app, None, "build_events"):
        built = build_events(app, fetch)
    evs = events(app)
    if prices:
        first = min(date.fromisoformat(e["pre_session"]) for e in evs) if evs else today
        if not one(app.conn, "SELECT 1 FROM sr_price_check WHERE subject='SPY' AND status='OK'"):
            with _Timer(app, None, "benchmarks"):
                pricing.fetch_benchmarks(app, history, date(first.year - 1, 1, 1), today)
    summary = {"events": len(evs), "built": built}
    for e in evs:
        sub = None
        if fetch is not None:
            with _Timer(app, e["id"], "submissions_index"):
                sub = submissions(app, e["cik"], fetch)
        if not one(app.conn, "SELECT 1 FROM sr_source WHERE event_id=? AND role LIKE 'LATEST_%'", (e["id"],)) and sub:
            with _Timer(app, e["id"], "periodic_reports"):
                records.attach_periodic_reports(app, e, sub, fetch)
        if not one(app.conn, "SELECT 1 FROM sr_fact WHERE event_id=? AND section='C_FINANCING'", (e["id"],)):
            with _Timer(app, e["id"], "xbrl_financing"):
                records.ingest_financials(app, e, fetch_facts)
                records.financing_record(app, e)
        if not one(app.conn, "SELECT 1 FROM sr_fact WHERE event_id=? AND author='DETERMINISTIC_KEYWORD'", (e["id"],)):
            with _Timer(app, e["id"], "candidate_statements"):
                records.candidate_statements(app, e)
        if prices:
            with _Timer(app, e["id"], "prices"):
                pricing.fetch_event_prices(app, e, history, today, sub)
            if not one(app.conn, "SELECT 1 FROM sr_eligibility WHERE event_id=?", (e["id"],)):
                pricing.eligibility(app, e)
            if not one(app.conn, "SELECT 1 FROM sr_outcome_audit WHERE event_id=?", (e["id"],)):
                pricing.outcome_audit(app, e, sub, today)
    return summary
