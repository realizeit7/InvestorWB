"""Scheduler adapter: computes due job instances in America/New_York and runs each at most once.

Scheduled polling is NOT real-time monitoring. A stopped process cannot monitor; ``eqm serve``
(or cron/systemd calling ``eqm jobs run-due``) must be running. See docs/RUNBOOK.md.

Idempotency: every instance has key ``<job>@<scheduled_for UTC>`` (UNIQUE in ``job_run``), so a
restart or a second process cannot run the same instance twice. A RUNNING row older than
``stale_after`` is treated as interrupted and retried (attempt + 1, max 3).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable

from ..app import App
from ..data import calendar as cal
from ..db.core import insert, one
from ..util import NY, iso_utc, new_id, parse_utc, to_json


@dataclass(frozen=True)
class JobSpec:
    name: str
    kind: str          # "session_daily" | "weekly" | "monthly_first_session"
    at: time           # New York wall-clock time
    weekday: int | None = None   # for weekly (Mon=0)
    description: str = ""


DEFAULT_JOBS = [
    JobSpec("daily_refresh", "session_daily", time(18, 30),
            description="After each session's data is available: prices, filings, affected companies, health"),
    JobSpec("weekly_digest", "weekly", time(9, 0), weekday=5, description="Saturday portfolio digest"),
    JobSpec("monthly_allocation", "monthly_first_session", time(9, 0),
            description="First session of the month: contribution confirmation + allocation proposal"),
]

MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(hours=2)


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=NY)      # zoneinfo resolves the correct EST/EDT offset


def occurs_on(spec: JobSpec, d: date) -> bool:
    if spec.kind == "session_daily":
        return cal.is_session(d)
    if spec.kind == "weekly":
        return d.weekday() == spec.weekday
    if spec.kind == "monthly_first_session":
        return cal.is_session(d) and cal.session_on_or_after(d.replace(day=1)) == d
    raise ValueError(spec.kind)


def latest_due(spec: JobSpec, now: datetime, lookback_days: int = 40) -> datetime | None:
    d = now.astimezone(NY).date()
    for _ in range(lookback_days):
        if occurs_on(spec, d) and _at(d, spec.at) <= now:
            return _at(d, spec.at)
        d -= timedelta(days=1)
    return None


def next_run(spec: JobSpec, now: datetime) -> datetime:
    d = now.astimezone(NY).date()
    for _ in range(400):
        if occurs_on(spec, d) and _at(d, spec.at) > now:
            return _at(d, spec.at)
        d += timedelta(days=1)
    raise RuntimeError("no next run found")


Handler = Callable[[App, str, datetime], dict]


def run_instance(app: App, spec: JobSpec, scheduled_for: datetime, handler: Handler, *, force: bool = False) -> dict:
    key = f"{spec.name}@{iso_utc(scheduled_for)}"
    row = one(app.conn, "SELECT * FROM job_run WHERE idempotency_key=?", (key,))
    attempt = 1
    if row:
        st = row["status"]
        if st in ("SUCCESS", "PARTIAL", "SKIPPED") and not force:
            return {"job": spec.name, "status": "ALREADY_DONE", "run_id": row["id"]}
        if st == "RUNNING" and app.now() - parse_utc(row["started_at"]) < STALE_AFTER:
            return {"job": spec.name, "status": "IN_PROGRESS", "run_id": row["id"]}
        if st in ("FAILED", "RUNNING") and row["attempt"] >= MAX_ATTEMPTS and not force:
            return {"job": spec.name, "status": "GAVE_UP", "run_id": row["id"]}
        attempt = row["attempt"] + 1
        app.conn.execute("UPDATE job_run SET status='RUNNING', started_at=?, finished_at=NULL, attempt=?, error=? WHERE id=?",
                         (app.now_iso(), attempt, "previous attempt interrupted" if st == "RUNNING" else row["error"], row["id"]))
        run_id = row["id"]
    else:
        run_id = new_id("job")
        insert(app.conn, "job_run", {"id": run_id, "job_name": spec.name, "scheduled_for": iso_utc(scheduled_for),
                                     "idempotency_key": key, "started_at": app.now_iso(), "finished_at": None,
                                     "status": "RUNNING", "attempt": 1, "detail_json": None, "error": None})
    try:
        detail = handler(app, run_id, scheduled_for) or {}
        status = "PARTIAL" if detail.get("failures") else "SUCCESS"
        app.conn.execute("UPDATE job_run SET status=?, finished_at=?, detail_json=? WHERE id=?",
                         (status, app.now_iso(), to_json(detail), run_id))
        return {"job": spec.name, "status": status, "run_id": run_id, "detail": detail}
    except Exception as exc:  # noqa: BLE001 - failures must be recorded, never swallowed silently
        app.conn.execute("UPDATE job_run SET status='FAILED', finished_at=?, error=? WHERE id=?",
                         (app.now_iso(), f"{type(exc).__name__}: {exc}", run_id))
        return {"job": spec.name, "status": "FAILED", "run_id": run_id, "error": str(exc)}


def run_due(app: App, handlers: dict[str, Handler], specs: list[JobSpec] = DEFAULT_JOBS) -> list[dict]:
    """Run the latest due instance of each job (older missed instances are not replayed)."""
    now = app.now()
    out = []
    for spec in specs:
        due = latest_due(spec, now)
        if due is None or spec.name not in handlers:
            continue
        out.append(run_instance(app, spec, due, handlers[spec.name]))
    return out


def status(app: App, specs: list[JobSpec] = DEFAULT_JOBS) -> list[dict]:
    now = app.now()
    out = []
    for s in specs:
        last = one(app.conn, "SELECT * FROM job_run WHERE job_name=? ORDER BY started_at DESC LIMIT 1", (s.name,))
        last_ok = one(app.conn, "SELECT * FROM job_run WHERE job_name=? AND status IN ('SUCCESS','PARTIAL') "
                                "ORDER BY started_at DESC LIMIT 1", (s.name,))
        due = latest_due(s, now)
        missed = due is not None and not one(app.conn, "SELECT 1 FROM job_run WHERE idempotency_key=?",
                                             (f"{s.name}@{iso_utc(due)}",))
        out.append({"job": s.name, "description": s.description, "last_run": dict(last) if last else None,
                    "last_success": last_ok["finished_at"] if last_ok else None, "next_run": iso_utc(next_run(s, now)),
                    "latest_due_not_run": iso_utc(due) if missed else None})
    return out


def serve(app_factory: Callable[[], App], handlers: dict[str, Handler], poll_seconds: int = 60,
          deliver: Callable[[App], dict] | None = None, max_loops: int | None = None) -> None:  # pragma: no cover - loop
    loops = 0
    while max_loops is None or loops < max_loops:
        app = app_factory()
        try:
            for r in run_due(app, handlers):
                if r["status"] not in ("ALREADY_DONE", "IN_PROGRESS"):
                    print(f"{app.now_iso()} {r['job']}: {r['status']}", flush=True)
            if deliver:
                deliver(app)
        finally:
            app.conn.close()
        loops += 1
        _time.sleep(poll_seconds)
