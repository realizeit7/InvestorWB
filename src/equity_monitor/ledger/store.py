"""Ledger persistence: portfolios, accounts, append-only events, corrections, issues."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

from ..app import App
from ..db.core import all_rows, insert, one, transaction
from ..util import D, dstr, iso_utc, new_id, parse_date, stable_hash, to_json
from .replay import LedgerEvent, replay_account

PORTFOLIO_KINDS = ("ACTUAL", "PAPER", "FIXTURE", "HYPOTHETICAL")


class PortfolioMixError(ValueError):
    """Raised when actual, paper, fixture or hypothetical records would be combined."""


# ------------------------------------------------------------------ portfolios & accounts
def create_portfolio(app: App, name: str, kind: str, note: str | None = None) -> str:
    if kind not in PORTFOLIO_KINDS:
        raise ValueError(f"kind must be one of {PORTFOLIO_KINDS}")
    row = one(app.conn, "SELECT id, kind FROM portfolio WHERE name=?", (name,))
    if row:
        if row["kind"] != kind:
            raise PortfolioMixError(f"portfolio {name!r} already exists with kind {row['kind']}")
        return row["id"]
    pid = new_id("pf")
    insert(app.conn, "portfolio", {"id": pid, "name": name, "kind": kind, "base_currency": "USD",
                                   "created_at": app.now_iso(), "note": note})
    app.audit("portfolio.created", "portfolio", pid, {"name": name, "kind": kind})
    return pid


def create_account(app: App, portfolio_id: str, name: str, *, broker: str | None = None,
                   tax_status: str = "UNKNOWN", settlement_days: int = 1) -> str:
    row = one(app.conn, "SELECT id FROM account WHERE portfolio_id=? AND name=?", (portfolio_id, name))
    if row:
        return row["id"]
    aid = new_id("acct")
    insert(app.conn, "account", {"id": aid, "portfolio_id": portfolio_id, "name": name, "broker": broker,
                                 "tax_status": tax_status, "settlement_days": settlement_days,
                                 "created_at": app.now_iso()})
    app.audit("account.created", "account", aid, {"portfolio_id": portfolio_id, "name": name})
    return aid


def portfolio_by_name(app: App, name: str) -> dict:
    row = one(app.conn, "SELECT * FROM portfolio WHERE name=? OR id=?", (name, name))
    if row is None:
        raise KeyError(f"unknown portfolio {name!r}")
    return dict(row)


def portfolio_kind(app: App, portfolio_id: str) -> str:
    return one(app.conn, "SELECT kind FROM portfolio WHERE id=?", (portfolio_id,))["kind"]


def accounts_of(app: App, portfolio_id: str) -> list[dict]:
    return [dict(r) for r in all_rows(app.conn, "SELECT * FROM account WHERE portfolio_id=? ORDER BY name",
                                      (portfolio_id,))]


def account_portfolio(app: App, account_id: str) -> dict:
    row = one(app.conn, "SELECT p.* FROM portfolio p JOIN account a ON a.portfolio_id=p.id WHERE a.id=?",
              (account_id,))
    if row is None:
        raise KeyError(account_id)
    return dict(row)


def assert_same_kind(app: App, portfolio_ids: list[str]) -> str:
    kinds = {portfolio_kind(app, p) for p in portfolio_ids}
    if len(kinds) != 1:
        raise PortfolioMixError(f"refusing to combine portfolios of kinds {sorted(kinds)}")
    return kinds.pop()


# ------------------------------------------------------------------ events
@dataclass
class NewEvent:
    event_type: str
    trade_date: date
    security_id: str | None = None
    settle_date: date | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    fees: Decimal | None = None
    amount: Decimal | None = None
    currency: str = "USD"
    cost_basis_total: Decimal | None = None
    ratio_num: Decimal | None = None
    ratio_den: Decimal | None = None
    action_subtype: str | None = None
    link_group_id: str | None = None
    reverses_event_id: str | None = None
    replaces_event_id: str | None = None
    external_id: str | None = None
    occurred_at: datetime | None = None
    source_timezone: str | None = None
    note: str | None = None
    import_row: int | None = None
    dedupe_extra: str = ""       # e.g. occurrence index within an import file

    def dedupe_key(self, account_id: str) -> str:
        if self.external_id:
            return f"ext:{account_id}:{self.external_id}"
        return "h:" + stable_hash({
            "a": account_id, "t": self.event_type, "d": self.trade_date, "s": self.security_id,
            "q": dstr(self.quantity), "p": dstr(self.price), "f": dstr(self.fees), "amt": dstr(self.amount),
            "cb": dstr(self.cost_basis_total), "rn": dstr(self.ratio_num), "rd": dstr(self.ratio_den),
            "sub": self.action_subtype, "rev": self.reverses_event_id, "x": self.dedupe_extra,
        })


@dataclass
class RecordResult:
    inserted: list[str] = field(default_factory=list)
    duplicates: list[int | None] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


def row_to_event(r) -> LedgerEvent:
    return LedgerEvent(
        id=r["id"], seq=r["seq"], account_id=r["account_id"], event_type=r["event_type"],
        trade_date=date.fromisoformat(r["trade_date"]), security_id=r["security_id"],
        settle_date=parse_date(r["settle_date"]), quantity=D(r["quantity"]), price=D(r["price"]),
        fees=D(r["fees"]), amount=D(r["amount"]), cost_basis_total=D(r["cost_basis_total"]),
        ratio_num=D(r["ratio_num"]), ratio_den=D(r["ratio_den"]), action_subtype=r["action_subtype"],
        link_group_id=r["link_group_id"], reverses_event_id=r["reverses_event_id"],
        replaces_event_id=r["replaces_event_id"], import_row=r["import_row"],
    )


def load_events(app: App, account_id: str) -> list[LedgerEvent]:
    return [row_to_event(r) for r in all_rows(app.conn, "SELECT * FROM ledger_event WHERE account_id=? ORDER BY seq",
                                              (account_id,))]


def _next_seq(app: App) -> int:
    return (one(app.conn, "SELECT COALESCE(MAX(seq), 0) AS m FROM ledger_event")["m"] or 0) + 1


def settlement_days(app: App, account_id: str) -> int:
    return one(app.conn, "SELECT settlement_days FROM account WHERE id=?", (account_id,))["settlement_days"]


def record_events(app: App, account_id: str, new_events: list[NewEvent], *, batch_id: str | None = None,
                  recorded_by: str | None = None, allow_negative_cash: bool = True) -> RecordResult:
    """Validate and append events. Duplicates are skipped; invalid rows are rejected, not coerced.

    Broker imports keep ``allow_negative_cash=True``: negative cash there is a reconciliation case (an issue is
    raised and allocation is withheld). Paper execution passes False: a batch that would take cash below zero
    at any point is rejected as a whole, because paper trading never borrows."""
    res = RecordResult()
    acct = one(app.conn, "SELECT * FROM account WHERE id=?", (account_id,))
    if acct is None:
        raise KeyError(account_id)
    existing = load_events(app, account_id)
    candidates: list[tuple[NewEvent, LedgerEvent, str]] = []
    seen_keys: set[str] = set()
    seq = _next_seq(app)
    for ne in new_events:
        if ne.currency != "USD":
            res.rejected.append({"row": ne.import_row, "error": f"currency {ne.currency} unsupported (USD only)"})
            continue
        key = ne.dedupe_key(account_id)
        if key in seen_keys or one(app.conn, "SELECT 1 FROM ledger_event WHERE dedupe_key=?", (key,)):
            res.duplicates.append(ne.import_row)
            continue
        seen_keys.add(key)
        if ne.event_type == "REVERSAL":
            target = one(app.conn, "SELECT account_id FROM ledger_event WHERE id=?", (ne.reverses_event_id,))
            if target is None or target["account_id"] != account_id:
                res.rejected.append({"row": ne.import_row, "error": "REVERSAL must reference an event in this account"})
                continue
        eid = new_id("evt")
        le = LedgerEvent(id=eid, seq=seq, account_id=account_id, event_type=ne.event_type,
                         trade_date=ne.trade_date, security_id=ne.security_id, settle_date=ne.settle_date,
                         quantity=ne.quantity, price=ne.price, fees=ne.fees, amount=ne.amount,
                         cost_basis_total=ne.cost_basis_total, ratio_num=ne.ratio_num, ratio_den=ne.ratio_den,
                         action_subtype=ne.action_subtype, link_group_id=ne.link_group_id,
                         reverses_event_id=ne.reverses_event_id, replaces_event_id=ne.replaces_event_id,
                         import_row=ne.import_row)
        seq += 1
        candidates.append((ne, le, key))

    # Validate the whole resulting history; reject offending new rows only.
    state = replay_account(account_id, existing + [c[1] for c in candidates], strict=False,
                           settlement_days=acct["settlement_days"])
    new_ids = {c[1].id for c in candidates}
    if not allow_negative_cash and candidates and any(i["type"] == "NEGATIVE_CASH" for i in state.issues):
        neg = next(i for i in state.issues if i["type"] == "NEGATIVE_CASH")
        for c in candidates:
            res.rejected.append({"row": c[0].import_row, "error": f"insufficient cash: balance would reach "
                                 f"{neg['min_cash']} on {neg['on']} (no borrowing)"})
        return res
    bad = {i["event_id"]: i for i in state.issues if i["type"] == "REJECTED_EVENT" and i["event_id"] in new_ids}
    old_bad = [i for i in state.issues if i["type"] == "REJECTED_EVENT" and i["event_id"] not in new_ids]
    if old_bad:
        # A new back-dated event made an existing event invalid (e.g. a back-dated sale).
        offenders = [c for c in candidates]
        for c in offenders:
            res.rejected.append({"row": c[0].import_row, "error": "would invalidate existing history: "
                                 + old_bad[0]["error"]})
        return res

    with transaction(app.conn):
        for ne, le, key in candidates:
            if le.id in bad:
                res.rejected.append({"row": ne.import_row, "error": bad[le.id]["error"]})
                continue
            insert(app.conn, "ledger_event", {
                "id": le.id, "seq": le.seq, "account_id": account_id, "event_type": le.event_type,
                "security_id": le.security_id, "trade_date": le.trade_date.isoformat(),
                "settle_date": le.settle_date.isoformat() if le.settle_date else None,
                "occurred_at": iso_utc(ne.occurred_at), "source_timezone": ne.source_timezone,
                "quantity": dstr(le.quantity), "price": dstr(le.price), "fees": dstr(le.fees),
                "amount": dstr(le.amount), "currency": ne.currency, "cost_basis_total": dstr(le.cost_basis_total),
                "ratio_num": dstr(le.ratio_num), "ratio_den": dstr(le.ratio_den),
                "action_subtype": le.action_subtype, "link_group_id": le.link_group_id,
                "reverses_event_id": le.reverses_event_id, "replaces_event_id": le.replaces_event_id,
                "external_id": ne.external_id, "import_batch_id": batch_id, "import_row": ne.import_row,
                "dedupe_key": key, "note": ne.note, "recorded_at": app.now_iso(),
                "recorded_by": recorded_by or app.actor,
            })
            res.inserted.append(le.id)
            if le.event_type == "CORPORATE_ACTION":
                raise_issue(app, f"corp:{le.id}", account_id=account_id, security_id=le.security_id,
                            issue_type="UNSUPPORTED_CORPORATE_ACTION", severity="CRITICAL",
                            detail={"event_id": le.id, "subtype": le.action_subtype,
                                    "on": le.trade_date.isoformat(),
                                    "action": "Record the resulting positions manually (e.g. REVERSAL + OPENING_POSITION"
                                              " with basis) and then resolve this issue."})
        if res.inserted:
            app.audit("ledger.appended", "account", account_id, {"count": len(res.inserted), "batch": batch_id})
    refresh_account_issues(app, account_id)
    return res


def reverse_event(app: App, event_id: str, reason: str) -> RecordResult:
    target = one(app.conn, "SELECT * FROM ledger_event WHERE id=?", (event_id,))
    if target is None:
        raise KeyError(event_id)
    if one(app.conn, "SELECT 1 FROM ledger_event WHERE reverses_event_id=?", (event_id,)):
        raise ValueError(f"event {event_id} already reversed")
    return record_events(app, target["account_id"], [NewEvent(
        event_type="REVERSAL", trade_date=app.now().date(), reverses_event_id=event_id, note=reason)])


def correct_event(app: App, event_id: str, replacement: NewEvent, reason: str) -> RecordResult:
    """Reverse ``event_id`` and append ``replacement`` linked to it. Both rows stay in the audit trail."""
    target = one(app.conn, "SELECT * FROM ledger_event WHERE id=?", (event_id,))
    if target is None:
        raise KeyError(event_id)
    replacement = replace(replacement, replaces_event_id=event_id, note=(replacement.note or reason))
    with transaction(app.conn):
        r1 = reverse_event(app, event_id, reason)
        if r1.rejected:
            raise ValueError(r1.rejected)
        r2 = record_events(app, target["account_id"], [replacement])
        if r2.rejected:
            raise ValueError(f"replacement rejected: {r2.rejected}")
    return r2


# ------------------------------------------------------------------ reconciliation issues
def raise_issue(app: App, dedupe_key: str, *, account_id: str | None, security_id: str | None,
                issue_type: str, severity: str, detail: dict) -> str | None:
    row = one(app.conn, "SELECT id, status FROM reconciliation_issue WHERE dedupe_key=?", (dedupe_key,))
    if row:
        return row["id"]
    iid = new_id("rec")
    insert(app.conn, "reconciliation_issue", {
        "id": iid, "dedupe_key": dedupe_key, "account_id": account_id, "security_id": security_id,
        "issue_type": issue_type, "severity": severity, "detail_json": to_json(detail), "status": "OPEN",
        "created_at": app.now_iso(), "resolved_at": None, "resolution_note": None,
    })
    app.audit("reconciliation.issue", "reconciliation_issue", iid, {"type": issue_type})
    return iid


def resolve_issue(app: App, issue_id: str, note: str, status: str = "RESOLVED") -> None:
    if status not in ("RESOLVED", "ACKNOWLEDGED"):
        raise ValueError(status)
    app.conn.execute("UPDATE reconciliation_issue SET status=?, resolved_at=?, resolution_note=? WHERE id=?",
                     (status, app.now_iso(), note, issue_id))
    app.audit("reconciliation.resolve", "reconciliation_issue", issue_id, {"status": status, "note": note})


def open_issues(app: App, account_ids: list[str] | None = None, security_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM reconciliation_issue WHERE status='OPEN'"
    params: list = []
    if account_ids is not None:
        sql += f" AND (account_id IN ({','.join('?' for _ in account_ids)}) OR account_id IS NULL)"
        params += account_ids
    if security_id is not None:
        sql += " AND security_id=?"
        params.append(security_id)
    return [dict(r) for r in all_rows(app.conn, sql + " ORDER BY created_at", params)]


def refresh_account_issues(app: App, account_id: str) -> None:
    """Re-derive replay-level issues (negative cash) as reconciliation issues."""
    state = replay_account(account_id, load_events(app, account_id), strict=False,
                           settlement_days=settlement_days(app, account_id))
    for i in state.issues:
        if i["type"] == "NEGATIVE_CASH":
            raise_issue(app, f"negcash:{account_id}:{i['on']}", account_id=account_id, security_id=None,
                        issue_type="NEGATIVE_CASH", severity="WARNING",
                        detail={**i, "hint": "Cash went below zero: a deposit or opening cash balance is probably"
                                             " missing. No borrowing is assumed."})
