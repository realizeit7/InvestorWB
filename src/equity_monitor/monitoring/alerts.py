"""Detected events -> alerts (local inbox) -> delivery outbox.

- Events have deterministic keys, so re-detection after a restart is a no-op.
- Alerts are created from events. Non-critical duplicates within the cooldown are suppressed;
  CRITICAL events are never suppressed.
- The inbox is the ``alert`` table. Acknowledge/snooze only change status (logged); evidence stays.
- Outbox rows exist only for channels the owner enabled AND authorized.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..app import App
from ..db.core import all_rows, insert, one
from ..util import from_json, iso_utc, new_id, parse_utc, to_json


def record_event(app: App, event_key: str, event_type: str, *, severity: str, verified: bool,
                 security_id: str | None = None, issuer_id: str | None = None, public_at: str | None = None,
                 document_id: str | None = None, job_run_id: str | None = None, payload: dict | None = None) -> tuple[str, bool]:
    row = one(app.conn, "SELECT id FROM detected_event WHERE event_key=?", (event_key,))
    if row:
        return row["id"], False
    eid = new_id("evn")
    insert(app.conn, "detected_event", {
        "id": eid, "event_key": event_key, "event_type": event_type, "security_id": security_id, "issuer_id": issuer_id,
        "severity": severity, "verified": int(verified), "detected_at": app.now_iso(), "public_at": public_at,
        "document_id": document_id, "job_run_id": job_run_id, "payload_json": to_json(payload or {}),
    })
    return eid, True


def create_alert(app: App, alert_key: str, kind: str, title: str, body_md: str, *, severity: str,
                 event_id: str | None = None, security_id: str | None = None, cooldown_group: str | None = None) -> str | None:
    """Create an alert unless it already exists or (non-critical) falls inside the cooldown."""
    if one(app.conn, "SELECT 1 FROM alert WHERE alert_key=?", (alert_key,)):
        return None
    if severity != "CRITICAL" and cooldown_group:
        since = iso_utc(app.now() - timedelta(hours=app.policy.alerts.cooldown_hours))
        recent = one(app.conn, "SELECT id FROM alert WHERE kind=? AND security_id IS ? AND created_at>=? AND title LIKE ?",
                     (kind, security_id, since, f"{cooldown_group}%"))
        if recent:
            return None
    aid = new_id("alr")
    insert(app.conn, "alert", {"id": aid, "alert_key": alert_key, "kind": kind, "event_id": event_id,
                               "security_id": security_id, "severity": severity, "title": title, "body_md": body_md,
                               "created_at": app.now_iso(), "status": "NEW", "snoozed_until": None})
    enqueue_deliveries(app, aid)
    return aid


def _set_status(app: App, alert_id: str, status: str, note: str | None, snoozed_until: str | None = None) -> None:
    cur = one(app.conn, "SELECT status FROM alert WHERE id=?", (alert_id,))
    if cur is None:
        raise KeyError(alert_id)
    app.conn.execute("UPDATE alert SET status=?, snoozed_until=? WHERE id=?", (status, snoozed_until, alert_id))
    insert(app.conn, "alert_status_log", {"id": new_id("als"), "alert_id": alert_id, "at": app.now_iso(),
                                          "from_status": cur["status"], "to_status": status, "note": note})


def acknowledge(app: App, alert_id: str, note: str | None = None) -> None:
    _set_status(app, alert_id, "ACKNOWLEDGED", note)


def snooze(app: App, alert_id: str, until: datetime, note: str | None = None) -> None:
    _set_status(app, alert_id, "SNOOZED", note, iso_utc(until))


def inbox(app: App, include_acknowledged: bool = False) -> list[dict]:
    now = app.now_iso()
    rows = all_rows(app.conn, "SELECT * FROM alert ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'MATERIAL' THEN 1 "
                              "ELSE 2 END, created_at DESC")
    out = []
    for r in rows:
        if r["status"] == "ACKNOWLEDGED" and not include_acknowledged:
            continue
        if r["status"] == "SNOOZED" and r["snoozed_until"] and r["snoozed_until"] > now:
            continue
        out.append(dict(r))
    return out


# ------------------------------------------------------------------ outbox
def enabled_channels(app: App) -> list[str]:
    import os
    n = app.settings.notifications
    if n.webhook_enabled and n.webhook_authorized and os.environ.get(n.webhook_url_env):
        return ["webhook"]
    return []


def enqueue_deliveries(app: App, alert_id: str) -> None:
    for ch in enabled_channels(app):
        insert(app.conn, "delivery_outbox", {
            "id": new_id("out"), "alert_id": alert_id, "channel": ch, "idempotency_key": f"{alert_id}:{ch}",
            "status": "PENDING", "attempts": 0, "next_attempt_at": app.now_iso(), "last_error": None,
            "created_at": app.now_iso(), "sent_at": None,
        }, or_ignore=True)


def alert_payload(app: App, alert_id: str) -> dict:
    a = dict(one(app.conn, "SELECT * FROM alert WHERE id=?", (alert_id,)))
    return {"id": a["id"], "kind": a["kind"], "severity": a["severity"], "title": a["title"], "body_markdown": a["body_md"],
            "created_at": a["created_at"],
            "note": "Decision support only. This message cannot execute trades."}


def event_payload(app: App, event_id: str) -> dict:
    r = one(app.conn, "SELECT * FROM detected_event WHERE id=?", (event_id,))
    return {**dict(r), "payload": from_json(r["payload_json"])}


def record_feedback(app: App, alert_id: str, useful: bool, note: str | None = None) -> None:
    """Owner rates an alert's usefulness (for prospective alert-quality evaluation)."""
    insert(app.conn, "alert_feedback", {"id": new_id("afb"), "alert_id": alert_id, "useful": int(useful), "note": note,
                                        "at": app.now_iso()})


def create_test_alert(app: App) -> str:
    """An explicit, owner-initiated TEST alert (never generated automatically). It is delivered externally only if
    webhook delivery is enabled AND authorized AND the URL variable is present — same rules as every alert."""
    aid = create_alert(app, f"test:{new_id('t')}", "TEST", "InvestorWB test notification",
                       "This is an owner-requested delivery test. No investment information is included. "
                       "If you received this on your device, delivery works; confirm receipt with "
                       "`eqm alerts ack <id> --note received`.", severity="INFO")
    app.audit("alerts.test_created", "alert", aid, {})
    return aid
