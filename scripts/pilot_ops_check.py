"""Operational checks on a pilot data home (run after scripts/live_pilot.sh). Nothing leaves the machine:

1. restart/recovery: kill -9 a running `eqm jobs run daily_refresh`, restart immediately (must not duplicate the
   instance), then retry after the 2-hour stale window (simulated clock) — no duplicate events/alerts/recommendations;
   also start `eqm serve`, stop it, start it again: nothing re-runs.
2. delivery mechanics: a LOCAL HTTP receiver on 127.0.0.1 stands in for the webhook (this is NOT the owner-authorized
   test to the owner's real destination; that one must be run by the owner, see docs/PILOT_CHECKLIST.md).
3. backup -> restore into a new home -> checksums, integrity and row counts compared.

    uv run python scripts/pilot_ops_check.py /path/to/pilot_home /path/to/pilot_user.yaml
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from equity_monitor.app import open_app
from equity_monitor.monitoring import scheduler as sch
from equity_monitor.monitoring.jobs import handlers
from equity_monitor.ops import backup, restore
from equity_monitor.util import Clock

TABLES = ("ledger_event", "recommendation", "detected_event", "alert", "job_run", "financial_fact", "price_bar",
          "market_observation", "delivery_outbox", "delivery_attempt")


def counts(home: Path, settings: str) -> dict:
    app = open_app(home, settings_path=settings, policy_path="config/policy.example.yaml")
    out = {t: app.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    app.conn.close()
    return out


def eqm(home, settings, *args, env=None):
    return ["uv", "run", "eqm", "--home", str(home), "--settings", settings, "--policy", "config/policy.example.yaml", *args]


def recovery(home: Path, settings: str) -> dict:
    before = counts(home, settings)
    p = subprocess.Popen(eqm(home, settings, "jobs", "run", "daily_refresh", "--force"), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    time.sleep(6)                                          # inside the network refresh
    p.send_signal(signal.SIGKILL)
    p.wait()
    app = open_app(home, settings_path=settings, policy_path="config/policy.example.yaml")
    row = dict(app.conn.execute("SELECT status, attempt, started_at FROM job_run WHERE job_name='daily_refresh' "
                                "ORDER BY started_at DESC LIMIT 1").fetchone())
    spec = next(s for s in sch.DEFAULT_JOBS if s.name == "daily_refresh")
    immediate = sch.run_instance(app, spec, sch.latest_due(spec, app.now()), handlers()["daily_refresh"])["status"]
    app.conn.close()
    later = open_app(home, settings_path=settings, policy_path="config/policy.example.yaml",
                     clock=Clock(Clock().now() + timedelta(hours=2, minutes=5)))
    retried = sch.run_instance(later, spec, sch.latest_due(spec, later.now()), handlers()["daily_refresh"])
    after_row = dict(later.conn.execute("SELECT status, attempt, error FROM job_run WHERE job_name='daily_refresh' "
                                        "ORDER BY started_at DESC LIMIT 1").fetchone())
    later.conn.close()
    mid = counts(home, settings)
    # serve restart: start, stop, start again -> nothing due re-runs, no duplicates
    for _ in range(2):
        s = subprocess.Popen(eqm(home, settings, "serve", "--poll", "2"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)
        s.send_signal(signal.SIGTERM)
        try:
            s.wait(timeout=20)
        except subprocess.TimeoutExpired:
            s.kill()
    after = counts(home, settings)
    return {"killed_row": row, "restart_immediately": immediate, "retry_after_stale_window": retried["status"],
            "retried_row": after_row, "counts_before": before, "counts_after_retry": mid, "counts_after_serve_restarts": after,
            "duplicates_after_serve_restarts": {t: after[t] - mid[t] for t in TABLES if after[t] != mid[t]}}


class _Receiver(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Receiver.got.append({"idempotency": self.headers.get("Idempotency-Key"), "body": json.loads(body or b"{}")})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def local_delivery(home: Path, settings: str) -> dict:
    srv = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = Path(home) / "pilot_user_local_webhook.yaml"
    base = Path(settings).read_text().replace("webhook_enabled: false", "webhook_enabled: true") \
        .replace("webhook_authorized: false", "webhook_authorized: true")
    cfg.write_text(base)
    env = {**os.environ, "EQM_WEBHOOK_URL": f"http://127.0.0.1:{srv.server_port}/hook", "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost"}
    first = subprocess.run(eqm(home, str(cfg), "alerts", "test"), env=env, capture_output=True, text=True)
    again = subprocess.run(eqm(home, str(cfg), "alerts", "deliver"), env=env, capture_output=True, text=True)
    srv.shutdown()
    cfg.unlink()
    return {"alerts_test_output": first.stdout.strip().splitlines()[-3:], "redeliver_output": again.stdout.strip(),
            "received": [{"idempotency": g["idempotency"], "title": g["body"]["alert"]["title"], "kind": g["body"]["alert"]["kind"]}
                         for g in _Receiver.got]}


def backup_restore(home: Path, settings: str) -> dict:
    app = open_app(home, settings_path=settings, policy_path="config/policy.example.yaml")
    archive = backup(app, Path(home) / "backups")
    app.conn.close()
    target = Path(str(home) + "_restored")
    res = restore(archive, target, force=True)
    a, b = counts(home, settings), counts(target, settings)
    return {"archive": archive.name, "restore": {k: v for k, v in res.items() if k != "files"},
            "row_counts_equal": a == b, "differences": {t: (a[t], b[t]) for t in TABLES if a[t] != b[t]}}


if __name__ == "__main__":
    home, settings = Path(sys.argv[1]), sys.argv[2]
    out = {"recovery": recovery(home, settings), "local_delivery": local_delivery(home, settings),
           "backup_restore": backup_restore(home, settings)}
    print(json.dumps(out, indent=1, default=str))
