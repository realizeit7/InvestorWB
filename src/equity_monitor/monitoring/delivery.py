"""External delivery: one webhook adapter (HTTP POST JSON) behind a persistent outbox.

Disabled unless ``notifications.webhook_enabled`` AND ``notifications.webhook_authorized`` are true
and the URL environment variable is set. Semantics:
- ``Idempotency-Key`` header = outbox key, stable across retries, so a receiver can de-duplicate.
- 2xx -> SENT. 408/429/5xx/connection errors -> FAILED with exponential backoff.
- Other 4xx -> DEAD (permanent; needs configuration fix).
- Read timeout after sending -> AMBIGUOUS: the receiver may or may not have the message. With
  ``ambiguous_timeout_policy: hold_for_review`` (default) the row is HELD until the owner re-queues it;
  with ``retry_same_key`` it is retried with the SAME key (safe only if the receiver de-duplicates).
- A SENT row is never sent again. Attempts beyond ``max_attempts`` -> DEAD.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Callable

import requests

from ..app import App
from ..db.core import all_rows, insert, one
from ..util import iso_utc, new_id, parse_utc
from .alerts import alert_payload


class AmbiguousTimeout(Exception):
    """The request was sent but no response arrived (the receiver may have processed it)."""


Transport = Callable[[str, dict, dict, float], int]


def requests_transport(url: str, body: dict, headers: dict, timeout: float) -> int:
    try:
        r = requests.post(url, json=body, headers=headers, timeout=(5, timeout))
    except requests.exceptions.ReadTimeout as exc:
        raise AmbiguousTimeout(str(exc)) from exc
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(str(exc)) from exc
    return r.status_code


def deliver_pending(app: App, transport: Transport | None = None, timeout: float = 15.0) -> dict:
    n = app.settings.notifications
    url = os.environ.get(n.webhook_url_env)
    transport = transport or requests_transport
    now = app.now()
    stats = {"sent": 0, "failed": 0, "ambiguous": 0, "dead": 0, "skipped": 0}
    rows = all_rows(app.conn, "SELECT * FROM delivery_outbox WHERE status IN ('PENDING','FAILED','AMBIGUOUS') "
                              "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at", (iso_utc(now),))
    for r in rows:
        if r["status"] == "AMBIGUOUS" and n.ambiguous_timeout_policy != "retry_same_key":
            stats["skipped"] += 1
            continue
        if not url or not (n.webhook_enabled and n.webhook_authorized):
            stats["skipped"] += 1
            continue
        attempts = r["attempts"] + 1
        headers = {"Idempotency-Key": r["idempotency_key"], "Content-Type": "application/json",
                   "User-Agent": "equity-monitor/0.1"}
        outcome, status_code, err, new_status = "SENT", None, None, "SENT"
        try:
            status_code = transport(url, {"idempotency_key": r["idempotency_key"], "alert": alert_payload(app, r["alert_id"])},
                                    headers, timeout)
            if 200 <= status_code < 300:
                outcome, new_status = "SENT", "SENT"
            elif status_code in (408, 429) or status_code >= 500:
                outcome, new_status, err = "RETRYABLE_ERROR", "FAILED", f"HTTP {status_code}"
            else:
                outcome, new_status, err = "PERMANENT_ERROR", "DEAD", f"HTTP {status_code}"
        except AmbiguousTimeout as exc:
            outcome, err = "AMBIGUOUS_TIMEOUT", str(exc)
            new_status = "AMBIGUOUS" if n.ambiguous_timeout_policy == "retry_same_key" else "HELD"
        except (ConnectionError, OSError) as exc:
            outcome, new_status, err = "RETRYABLE_ERROR", "FAILED", str(exc)
        if new_status in ("FAILED", "AMBIGUOUS") and attempts >= n.max_attempts:
            new_status = "DEAD"
        next_at = iso_utc(now + timedelta(minutes=2 ** attempts)) if new_status in ("FAILED", "AMBIGUOUS") else None
        insert(app.conn, "delivery_attempt", {"id": new_id("att"), "outbox_id": r["id"], "attempted_at": app.now_iso(),
                                              "outcome": outcome, "http_status": status_code, "error": err})
        app.conn.execute("UPDATE delivery_outbox SET status=?, attempts=?, next_attempt_at=?, last_error=?, sent_at=? "
                         "WHERE id=? AND status<>'SENT'",
                         (new_status, attempts, next_at, err, app.now_iso() if new_status == "SENT" else None, r["id"]))
        key = {"SENT": "sent", "FAILED": "failed", "AMBIGUOUS": "ambiguous", "HELD": "ambiguous", "DEAD": "dead"}[new_status]
        stats[key] += 1
    return stats


def requeue(app: App, outbox_id: str, note: str = "") -> None:
    """Owner decision after an ambiguous timeout: send again with the same idempotency key."""
    r = one(app.conn, "SELECT status FROM delivery_outbox WHERE id=?", (outbox_id,))
    if r["status"] == "SENT":
        raise ValueError("already sent")
    app.conn.execute("UPDATE delivery_outbox SET status='PENDING', next_attempt_at=? WHERE id=?", (app.now_iso(), outbox_id))
    app.audit("delivery.requeue", "delivery_outbox", outbox_id, {"note": note})
