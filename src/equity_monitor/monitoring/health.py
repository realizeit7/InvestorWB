"""System health: job status, provider status, data issues, costs, delivery, scheduler heartbeat."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.rawstore import open_quality_issues
from ..db.core import all_rows, one
from ..util import iso_utc, parse_utc
from .scheduler import DEFAULT_JOBS, status as job_status


def system_health(app: App) -> dict:
    now = app.now()
    warnings: list[str] = []
    jobs = job_status(app)
    for j in jobs:
        lr = j["last_run"]
        if lr and lr["status"] == "FAILED":
            warnings.append(f"job {j['job']} FAILED at {lr['started_at']}: {lr['error']}")
        if lr and lr["status"] == "PARTIAL":
            warnings.append(f"job {j['job']} finished PARTIAL at {lr['finished_at']} (some refreshes failed)")
        if j["latest_due_not_run"]:
            warnings.append(f"job {j['job']} was due at {j['latest_due_not_run']} but has not run — is the scheduler running?")
    from .scheduler import STALE_AFTER
    for r in all_rows(app.conn, "SELECT job_name, started_at, attempt FROM job_run WHERE status='RUNNING' AND started_at<?",
                      (iso_utc(now - STALE_AFTER),)):
        warnings.append(f"job {r['job_name']} has been RUNNING since {r['started_at']} (attempt {r['attempt']}): the process "
                        "was probably interrupted; the next scheduler pass retries it")
    last = one(app.conn, "SELECT MAX(started_at) AS t FROM job_run")["t"]
    if last is None:
        warnings.append("scheduler has never run: monitoring is NOT active (start `eqm serve` or a cron entry)")
    providers = []
    for r in all_rows(app.conn, "SELECT provider, check_type, MAX(CASE WHEN success=1 THEN checked_at END) AS ok, "
                                "MAX(CASE WHEN success=0 THEN checked_at END) AS bad, "
                                "SUM(CASE WHEN success=0 AND checked_at>=? THEN 1 ELSE 0 END) AS recent_fail "
                                "FROM source_check GROUP BY provider, check_type", (iso_utc(now - timedelta(days=7)),)):
        providers.append({"provider": r["provider"], "check_type": r["check_type"], "last_success": r["ok"],
                          "last_failure": r["bad"], "recent_failures": r["recent_fail"]})
        if r["bad"] and (r["ok"] is None or r["bad"] > r["ok"]):
            warnings.append(f"provider {r['provider']} {r['check_type']}: latest check FAILED ({r['bad']})")
    dq = open_quality_issues(app)
    for i in dq:
        if i["severity"] == "CRITICAL":
            warnings.append(f"data issue {i['issue_code']} ({i['ref_id']}): {i['detail'][:120]}")
    session = cal.latest_completed_session(now)
    stale = all_rows(app.conn, "SELECT s.symbol, MAX(p.session_date) AS d FROM price_bar p JOIN security s ON s.id=p.security_id "
                               "GROUP BY s.id HAVING d < ?", (session.isoformat(),))
    for r in stale:
        warnings.append(f"price for {r['symbol']} last dated {r['d']} (latest completed session {session})")
    month = now.strftime("%Y-%m")
    llm = one(app.conn, "SELECT COALESCE(SUM(CAST(amount_usd AS REAL)),0) AS s, SUM(CASE WHEN amount_usd IS NULL THEN 1 ELSE 0 END) AS u "
                        "FROM cost_record WHERE category='LLM' AND substr(occurred_at,1,7)=?", (month,))
    data = one(app.conn, "SELECT COALESCE(SUM(CAST(amount_usd AS REAL)),0) AS s FROM cost_record WHERE category='DATA' "
                         "AND substr(occurred_at,1,7)=?", (month,))
    outbox = {r["status"]: r["n"] for r in all_rows(app.conn, "SELECT status, COUNT(*) AS n FROM delivery_outbox GROUP BY status")}
    for bad in ("DEAD", "HELD"):
        if outbox.get(bad):
            warnings.append(f"{outbox[bad]} notification(s) in state {bad} need attention")
    overall = "OK" if not warnings else ("DEGRADED" if all("job" not in w or "PARTIAL" in w for w in warnings) else "ATTENTION")
    return {"overall": overall, "warnings": warnings, "jobs": jobs, "providers": providers, "data_issues": dq,
            "costs": {"llm_month": Decimal(str(llm["s"])), "llm_unknown": llm["u"] or 0, "data_month": Decimal(str(data["s"]))},
            "outbox": outbox, "latest_completed_session": session}
