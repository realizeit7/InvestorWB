"""Reconciliation against brokerage snapshots and provider corporate actions.

Discrepancies become ``reconciliation_issue`` rows. Nothing here modifies the ledger: the owner
decides whether the ledger or the snapshot is wrong and records a correction explicitly.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ..app import App
from ..data.prices import actions_for
from ..data.securities import register_security
from ..db.core import all_rows, insert, one
from ..util import D, dstr, new_id, sha256_bytes
from .replay import replay_account
from .store import load_events, raise_issue, settlement_days

QTY_TOLERANCE = Decimal("0.0001")
CASH_TOLERANCE = Decimal("0.01")


def import_snapshot(app: App, account_id: str, as_of: date, path: str | Path | None = None, *,
                    text: str | None = None, source: str = "broker_csv") -> str:
    """CSV columns: type (POSITION|CASH), symbol, quantity, cash, market_value, cost_basis."""
    data = Path(path).read_bytes() if text is None else text.encode()
    text = data.decode("utf-8-sig")
    h = sha256_bytes(data)
    row = one(app.conn, "SELECT id FROM brokerage_snapshot WHERE account_id=? AND as_of_date=? AND file_sha256=?",
              (account_id, as_of.isoformat(), h))
    if row:
        return row["id"]
    sid = new_id("snap")
    insert(app.conn, "brokerage_snapshot", {"id": sid, "account_id": account_id, "as_of_date": as_of.isoformat(),
                                            "source": source, "file_sha256": h, "imported_at": app.now_iso()})
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items()}
        t = r.get("type", "POSITION").upper()
        sec = register_security(app.conn, app.now_iso(), r["symbol"], source="snapshot") if r.get("symbol") else None
        insert(app.conn, "brokerage_snapshot_line", {
            "snapshot_id": sid, "line_type": t, "security_id": sec, "quantity": dstr(D(r.get("quantity"))),
            "cash": dstr(D(r.get("cash"))), "market_value": dstr(D(r.get("market_value"))),
            "cost_basis_total": dstr(D(r.get("cost_basis"))),
        })
    app.audit("reconciliation.snapshot_imported", "brokerage_snapshot", sid, {"as_of": as_of.isoformat()})
    return sid


def reconcile_snapshot(app: App, snapshot_id: str) -> list[dict]:
    snap = one(app.conn, "SELECT * FROM brokerage_snapshot WHERE id=?", (snapshot_id,))
    account_id = snap["account_id"]
    as_of = date.fromisoformat(snap["as_of_date"])
    state = replay_account(account_id, load_events(app, account_id), as_of=as_of, strict=False,
                           settlement_days=settlement_days(app, account_id))
    lines = all_rows(app.conn, "SELECT * FROM brokerage_snapshot_line WHERE snapshot_id=?", (snapshot_id,))
    found = []
    broker_pos: dict[str, Decimal] = {}
    broker_cash: Decimal | None = None
    for l in lines:
        if l["line_type"] == "CASH":
            broker_cash = (broker_cash or Decimal(0)) + (D(l["cash"]) or Decimal(0))
        else:
            broker_pos[l["security_id"]] = broker_pos.get(l["security_id"], Decimal(0)) + (D(l["quantity"]) or Decimal(0))
    ours = {sid: p.shares for sid, p in state.positions.items() if p.shares != 0}
    for sid in sorted(set(broker_pos) | set(ours)):
        b, o = broker_pos.get(sid, Decimal(0)), ours.get(sid, Decimal(0))
        if abs(b - o) > QTY_TOLERANCE:
            d = {"snapshot_id": snapshot_id, "as_of": as_of.isoformat(), "broker_quantity": str(b),
                 "ledger_quantity": str(o), "difference": str(b - o)}
            raise_issue(app, f"snapqty:{snapshot_id}:{sid}", account_id=account_id, security_id=sid,
                        issue_type="SNAPSHOT_QUANTITY_MISMATCH", severity="CRITICAL", detail=d)
            found.append({"type": "SNAPSHOT_QUANTITY_MISMATCH", "security_id": sid, **d})
    if broker_cash is not None and abs(broker_cash - state.cash) > CASH_TOLERANCE:
        d = {"snapshot_id": snapshot_id, "as_of": as_of.isoformat(), "broker_cash": str(broker_cash),
             "ledger_cash": str(state.cash), "difference": str(broker_cash - state.cash)}
        raise_issue(app, f"snapcash:{snapshot_id}", account_id=account_id, security_id=None,
                    issue_type="SNAPSHOT_CASH_MISMATCH", severity="CRITICAL", detail=d)
        found.append({"type": "SNAPSHOT_CASH_MISMATCH", **d})
    app.audit("reconciliation.run", "brokerage_snapshot", snapshot_id, {"issues": len(found)})
    return found


def check_provider_actions(app: App, account_id: str, as_of: date) -> list[dict]:
    """Compare provider corporate actions with ledger events for held securities."""
    events = load_events(app, account_id)
    state = replay_account(account_id, events, as_of=as_of, strict=False,
                           settlement_days=settlement_days(app, account_id))
    found = []
    for sid, pos in state.positions.items():
        acquired = [e.trade_date for e in events if e.security_id == sid and e.event_type in ("BUY", "OPENING_POSITION")]
        if not acquired:
            continue
        since = min(acquired)
        ledger_splits = [e.trade_date for e in events if e.security_id == sid and e.event_type == "SPLIT"]
        for a in actions_for(app, sid, since + timedelta(days=1), as_of):
            ex = date.fromisoformat(a["ex_date"])
            if a["action_type"] == "SPLIT":
                if not any(abs((ex - d).days) <= 5 for d in ledger_splits):
                    det = {"ex_date": a["ex_date"], "ratio": f"{a['ratio_num']}:{a['ratio_den']}",
                           "provider": a["provider"],
                           "action": "Add a SPLIT ledger event (or confirm the provider is wrong)."}
                    raise_issue(app, f"missingsplit:{account_id}:{sid}:{a['ex_date']}", account_id=account_id,
                                security_id=sid, issue_type="MISSING_SPLIT_EVENT", severity="CRITICAL", detail=det)
                    found.append({"type": "MISSING_SPLIT_EVENT", "security_id": sid, **det})
            elif a["action_type"] in ("MERGER", "SPINOFF", "DELISTING", "OTHER", "SYMBOL_CHANGE"):
                det = {"ex_date": a["ex_date"], "action_type": a["action_type"], "provider": a["provider"],
                       "action": "Unsupported corporate action: record resulting positions manually."}
                raise_issue(app, f"provideraction:{account_id}:{sid}:{a['action_type']}:{a['ex_date']}",
                            account_id=account_id, security_id=sid, issue_type="UNSUPPORTED_CORPORATE_ACTION",
                            severity="CRITICAL", detail=det)
                found.append({"type": "UNSUPPORTED_CORPORATE_ACTION", "security_id": sid, **det})
    return found
