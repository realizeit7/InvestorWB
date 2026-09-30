"""Monitoring operations (§18.21-24)."""

import json
import os
from datetime import date, datetime, time, timedelta, timezone

import pytest

from equity_monitor.config.models import NotificationSettings, UserSettings
from equity_monitor.data.prices import Bar, FixturePriceProvider, PriceFetch
from equity_monitor.data.securities import get_or_create_issuer, register_security
from equity_monitor.decisions.recommend import latest_for, set_watchlist
from equity_monitor.fixtures import AS_OF, build_demo
from equity_monitor.monitoring import scheduler as sch
from equity_monitor.monitoring.alerts import acknowledge, create_alert, inbox, snooze
from equity_monitor.monitoring.delivery import AmbiguousTimeout, deliver_pending, requeue
from equity_monitor.monitoring.health import system_health
from equity_monitor.monitoring.jobs import JobContext, daily_refresh, handlers
from equity_monitor.util import iso_utc

UTC = timezone.utc


def _submissions(accessions):
    recent = {"accessionNumber": [], "filingDate": [], "reportDate": [], "acceptanceDateTime": [], "form": [],
              "primaryDocument": [], "items": []}
    for acc, form, items, when in accessions:
        recent["accessionNumber"].append(acc)
        recent["filingDate"].append(when[:10])
        recent["reportDate"].append("")
        recent["acceptanceDateTime"].append(when)
        recent["form"].append(form)
        recent["primaryDocument"].append("doc.htm")
        recent["items"].append(items)
    return json.dumps({"sic": "3571", "sicDescription": "x", "fiscalYearEnd": "0930", "filings": {"recent": recent}}).encode()


@pytest.fixture
def demo(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="Real Test Co", cik="0000123456")
    sid = register_security(app.conn, app.now_iso(), "RTC", security_type="COMMON", issuer_id=iss)
    set_watchlist(app, sid, "RESEARCH")
    d["real"] = {"issuer_id": iss, "security_id": sid}
    return d


def _prices(demo, extra=None, fail=None):
    data = {s: PriceFetch([Bar(date(2026, 9, 30), v.get("price") or 30)]) for s, v in demo["securities"].items()}
    data["RTC"] = PriceFetch([Bar(date(2026, 9, 30), 10)])
    data.update(extra or {})
    return FixturePriceProvider(data, fail=fail or set())


def test_restart_does_not_duplicate_events(app, demo):  # §18.21
    subs = {demo["real"]["issuer_id"]: _submissions([("0001-26-000001", "8-K", "1.03", "2026-09-30T20:45:00.000Z"),
                                                     ("0001-26-000002", "10-Q", "", "2026-09-29T20:00:00.000Z")])}
    ctx = JobContext(price_provider=_prices(demo), submissions=subs)
    h = handlers(ctx)
    spec = sch.DEFAULT_JOBS[0]
    due = sch.latest_due(spec, app.now())
    r1 = sch.run_instance(app, spec, due, h["daily_refresh"])
    assert r1["status"] in ("SUCCESS", "PARTIAL")
    n_events = app.conn.execute("SELECT COUNT(*) FROM detected_event").fetchone()[0]
    n_alerts = app.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0]
    assert n_events >= 2
    # "restart": the same instance again is a no-op; a forced re-run re-detects nothing new
    assert sch.run_instance(app, spec, due, h["daily_refresh"])["status"] == "ALREADY_DONE"
    sch.run_instance(app, spec, due, h["daily_refresh"], force=True)
    assert app.conn.execute("SELECT COUNT(*) FROM detected_event").fetchone()[0] == n_events
    assert app.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == n_alerts
    crit = app.conn.execute("SELECT * FROM detected_event WHERE severity='CRITICAL'").fetchone()
    assert crit["event_type"] == "NEW_FILING" and "Bankruptcy" in json.loads(crit["payload_json"])["label"]


def test_interrupted_run_is_retried_not_duplicated(app, demo):
    spec = sch.DEFAULT_JOBS[1]
    due = sch.latest_due(spec, app.now())
    calls = []

    def crashing(app_, rid, when):
        calls.append(rid)
        raise RuntimeError("boom")
    assert sch.run_instance(app, spec, due, crashing)["status"] == "FAILED"
    assert sch.run_instance(app, spec, due, lambda a, r, w: {"ok": 1})["status"] == "SUCCESS"
    rows = app.conn.execute("SELECT * FROM job_run WHERE job_name=?", (spec.name,)).fetchall()
    assert len(rows) == 1 and rows[0]["attempt"] == 2


def test_failed_refresh_is_visible(app, demo):  # §18.22
    zzhld = demo["securities"]["ZZHLD"]["security_id"]
    ctx = JobContext(price_provider=_prices(demo, fail={"ZZHLD"}))
    app.clock.set(AS_OF + timedelta(days=1))   # 2026-10-01 18:00 ET: a new session needs prices
    res = sch.run_instance(app, sch.DEFAULT_JOBS[0], sch.latest_due(sch.DEFAULT_JOBS[0], app.now()), handlers(ctx)["daily_refresh"])
    assert res["status"] == "PARTIAL"
    assert any(zzhld in f for f in res["detail"]["failures"])
    h = system_health(app)
    assert h["overall"] != "OK"
    assert any("PRICE_REFRESH_FAILED" in w for w in h["warnings"])
    alerts = [a for a in inbox(app) if a["kind"] == "HEALTH"]
    assert alerts, "failure must appear in the inbox"
    rec = latest_for(app, demo["portfolio_id"], zzhld)
    assert rec["action"] == "REVIEW" and "DATA_REFRESH_FAILED" in rec["reason_codes"]


def test_scheduler_not_running_is_reported(app):
    h = system_health(app)
    assert any("never run" in w for w in h["warnings"])


def test_schedule_dst_boundaries():  # §18.24
    daily = sch.DEFAULT_JOBS[0]
    # 18:30 ET is 22:30Z in EDT and 23:30Z in EST
    fri_edt = sch.next_run(daily, datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    mon_est = sch.next_run(daily, datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    assert fri_edt == datetime(2026, 10, 30, 22, 30, tzinfo=UTC)
    assert mon_est == datetime(2026, 11, 2, 23, 30, tzinfo=UTC)    # skips the weekend, EST after Nov 1
    # no daily run on holidays; monthly = first session of the month
    assert sch.next_run(daily, datetime(2026, 11, 26, 12, tzinfo=UTC)).date() == date(2026, 11, 27)
    monthly = sch.DEFAULT_JOBS[2]
    assert sch.next_run(monthly, datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 4, 14, 0, tzinfo=UTC)
    weekly = sch.DEFAULT_JOBS[1]
    assert sch.next_run(weekly, datetime(2026, 3, 2, tzinfo=UTC)) == datetime(2026, 3, 7, 14, 0, tzinfo=UTC)   # EST
    assert sch.next_run(weekly, datetime(2026, 3, 9, tzinfo=UTC)) == datetime(2026, 3, 14, 13, 0, tzinfo=UTC)  # EDT


# ------------------------------------------------------------------ delivery
@pytest.fixture
def webhook_app(app, monkeypatch):
    monkeypatch.setenv("EQM_WEBHOOK_URL", "https://hooks.example.test/x")
    app.settings = UserSettings(notifications=NotificationSettings(webhook_enabled=True, webhook_authorized=True, max_attempts=3))
    return app


def test_delivery_disabled_without_authorization(app, monkeypatch):
    monkeypatch.setenv("EQM_WEBHOOK_URL", "https://hooks.example.test/x")
    app.settings = UserSettings(notifications=NotificationSettings(webhook_enabled=True, webhook_authorized=False))
    create_alert(app, "k1", "HEALTH", "t", "b", severity="CRITICAL")
    assert app.conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0
    assert len(inbox(app)) == 1


def test_delivery_retries_and_dedup(webhook_app):  # §18.23
    app = webhook_app
    sent = []
    responses = iter([503, 200])

    def transport(url, body, headers, timeout):
        sent.append(headers["Idempotency-Key"])
        return next(responses)
    create_alert(app, "k1", "MATERIAL_EVENT", "t", "b", severity="MATERIAL")
    create_alert(app, "k1", "MATERIAL_EVENT", "t", "b", severity="MATERIAL")     # duplicate key: ignored
    assert deliver_pending(app, transport)["failed"] == 1
    assert deliver_pending(app, transport)["sent"] == 0                          # backoff: not yet due
    app.clock.set(app.now() + timedelta(minutes=5))
    assert deliver_pending(app, transport)["sent"] == 1
    assert deliver_pending(app, transport) == {"sent": 0, "failed": 0, "ambiguous": 0, "dead": 0, "skipped": 0}
    assert len(sent) == 2 and sent[0] == sent[1]                                 # same idempotency key on retry


def test_ambiguous_timeout_is_held(webhook_app):
    app = webhook_app

    def timeout(url, body, headers, t):
        raise AmbiguousTimeout("read timeout")
    create_alert(app, "k2", "MATERIAL_EVENT", "t", "b", severity="MATERIAL")
    assert deliver_pending(app, timeout)["ambiguous"] == 1
    row = app.conn.execute("SELECT * FROM delivery_outbox").fetchone()
    assert row["status"] == "HELD"
    app.clock.set(app.now() + timedelta(hours=1))
    assert deliver_pending(app, lambda *a: 200)["sent"] == 0                     # never auto-resent
    requeue(app, row["id"], "receiver confirmed nothing arrived")
    assert deliver_pending(app, lambda *a: 200)["sent"] == 1


def test_permanent_error_and_max_attempts(webhook_app):
    app = webhook_app
    create_alert(app, "k3", "HEALTH", "t", "b", severity="CRITICAL")
    assert deliver_pending(app, lambda *a: 404)["dead"] == 1
    create_alert(app, "k4", "HEALTH", "t", "b", severity="CRITICAL")
    for _ in range(3):
        app.clock.set(app.now() + timedelta(hours=1))
        deliver_pending(app, lambda *a: 500)
    assert app.conn.execute("SELECT status FROM delivery_outbox WHERE idempotency_key LIKE '%'"
                            " ORDER BY created_at DESC LIMIT 1").fetchone()["status"] == "DEAD"


def test_cooldown_never_suppresses_critical(app):
    assert create_alert(app, "a1", "MATERIAL_EVENT", "X: item", "b", severity="MATERIAL", cooldown_group="X: ")
    assert create_alert(app, "a2", "MATERIAL_EVENT", "X: other", "b", severity="MATERIAL", cooldown_group="X: ") is None
    assert create_alert(app, "a3", "MATERIAL_EVENT", "X: bankruptcy", "b", severity="CRITICAL", cooldown_group="X: ")


def test_acknowledge_and_snooze_keep_evidence(app):
    a = create_alert(app, "s1", "HEALTH", "t", "body", severity="MATERIAL")
    snooze(app, a, app.now() + timedelta(days=1))
    assert inbox(app) == []
    app.clock.set(app.now() + timedelta(days=2))
    assert len(inbox(app)) == 1
    acknowledge(app, a, "seen")
    assert inbox(app) == [] and len(inbox(app, include_acknowledged=True)) == 1
    assert app.conn.execute("SELECT COUNT(*) FROM alert_status_log WHERE alert_id=?", (a,)).fetchone()[0] == 2
