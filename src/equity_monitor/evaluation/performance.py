"""Prospective evaluation: contribution-matched benchmarks, performance, process quality.

Benchmark construction (POLICY.md §7, stated assumptions):
- The hypothetical benchmark receives the same EXTERNAL flows as the portfolio on the same dates:
  deposits/opening cash buy the benchmark, withdrawals sell it. In-kind opening positions are
  converted at their market value on the opening date (unknown if the position has no price).
- Execution: at the raw close of the first session on or after the flow date; zero fees and zero
  slippage by default (``fee_bps`` configurable). Fractional units allowed.
- Dividends are reinvested at the ex-date close; splits multiply units. Raw prices + explicit
  actions => no double counting.
- Subperiods: the benchmark is replayed from INCEPTION (the first external flow) and then sliced to
  [start, end], so money contributed before ``start`` is invested in the benchmark too (mode "inception").
  ``mode="rebased"`` instead starts the benchmark at ``start`` with the portfolio's NAV on the last
  session before ``start`` and is labelled REBASED.
- A flow on a non-trading day executes at the next session; if that session has no benchmark bar, it
  executes at the next available bar (warning: late execution); with no bar through ``end`` it is
  unapplied. A withdrawal larger than the benchmark value empties it (warning) — units never go negative.
All figures are PRE-TAX. Cash drag is part of portfolio performance by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.prices import actions_for, price_on_or_before, price_series
from ..data.securities import find_security
from ..db.core import all_rows, insert, one
from ..ledger.views import portfolio_view
from ..util import new_id, to_json
from .returns import max_drawdown, time_weighted_return, xirr

ZERO = Decimal(0)


@dataclass
class BenchmarkResult:
    symbol: str
    values: list[tuple[date, Decimal]]
    units: Decimal
    flows_applied: list[dict]
    unapplied: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mode: str = "inception"                    # inception (replayed from first flow, then sliced) | rebased
    replay_start: date | None = None


def external_flows(app: App, portfolio_id: str, end: date) -> list[tuple[date, Decimal | None, str]]:
    """(date, amount, kind) with in-kind opening positions valued at market (None if no price)."""
    view = portfolio_view(app, portfolio_id, end)
    out = []
    for f in view.external_flows:
        if f.amount is not None:
            out.append((f.on, f.amount, f.kind))
        else:
            px = price_on_or_before(app, f.security_id, f.on)
            out.append((f.on, (px[1] * f.quantity) if px else None, f.kind))
    return sorted(out, key=lambda x: x[0])


def contribution_matched(app: App, portfolio_id: str, symbol: str, start: date, end: date, fee_bps: Decimal = ZERO,
                         mode: str = "inception") -> BenchmarkResult:
    if mode not in ("inception", "rebased"):
        raise ValueError("mode must be 'inception' or 'rebased'")
    sid = find_security(app.conn, symbol)
    if sid is None:
        raise KeyError(f"benchmark {symbol} has no price data; refresh prices for it first")
    flows = external_flows(app, portfolio_id, end)
    warnings: list[str] = []
    unapplied, applied = [], []
    pending: list[tuple[date, Decimal, str]] = []
    if mode == "inception":
        replay_start = min([d for d, _a, _k in flows] + [start])
        for d, amt, kind in flows:
            if d > end:
                continue
            if amt is None:
                unapplied.append({"date": d, "kind": kind, "reason": "in-kind position without a price"})
                continue
            pending.append((cal.session_on_or_after(d), amt, kind))
    else:
        replay_start = start
        base_day = cal.previous_session(cal.session_on_or_after(start))
        nav0 = portfolio_view(app, portfolio_id, base_day).nav
        if nav0 is None:
            warnings.append(f"portfolio NAV on {base_day} unknown: rebased benchmark starts empty")
        elif nav0 > 0:
            pending.append((cal.session_on_or_after(start), nav0, "REBASE"))
        for d, amt, kind in flows:
            if start <= d <= end:
                if amt is None:
                    unapplied.append({"date": d, "kind": kind, "reason": "in-kind position without a price"})
                else:
                    pending.append((cal.session_on_or_after(d), amt, kind))
    closes = price_series(app, sid, replay_start - timedelta(days=10), end)
    acts = {}
    for a_ in actions_for(app, sid, replay_start, end):
        acts.setdefault(date.fromisoformat(a_["ex_date"]), []).append(a_)
    pending.sort(key=lambda x: x[0])
    units = ZERO
    values = []
    for d in sorted(x for x in closes if replay_start <= x <= end):
        px = closes[d]
        for a_ in acts.get(d, []):
            if a_["action_type"] == "SPLIT":
                units *= Decimal(a_["ratio_num"]) / Decimal(a_["ratio_den"])
        for a_ in acts.get(d, []):
            if a_["action_type"] == "CASH_DIVIDEND" and px > 0:
                units += units * Decimal(a_["cash_amount"]) / px
        due = [p for p in pending if p[0] <= d]
        pending = [p for p in pending if p[0] > d]
        for exec_d, amt, kind in due:
            if exec_d < d:
                warnings.append(f"{kind} {amt} due {exec_d} executed late on {d}: no {symbol} bar on {exec_d}")
            if amt > 0:
                units += (amt - abs(amt) * fee_bps / Decimal(10000)) / px
            else:
                sell = -amt / px
                if sell > units:
                    warnings.append(f"withdrawal {amt} on {d} exceeds benchmark value {units * px:.2f}; benchmark emptied")
                    sell = units
                units -= sell
            applied.append({"date": d, "amount": amt, "price": px, "kind": kind})
        if d >= start:
            values.append((d, units * px))
    for exec_d, amt, kind in pending:
        unapplied.append({"date": exec_d, "amount": amt, "kind": kind,
                          "reason": "no benchmark price on/after flow date within range"})
    return BenchmarkResult(symbol, values, units, applied, unapplied, warnings, mode, replay_start)


def nav_series(app: App, portfolio_id: str, start: date, end: date) -> list[tuple[date, Decimal | None]]:
    return [(d, portfolio_view(app, portfolio_id, d).nav) for d in cal.sessions_between(start, end)]


def performance(app: App, portfolio_id: str, start: date, end: date, benchmarks: list[str] | None = None) -> dict:
    benchmarks = benchmarks if benchmarks is not None else app.settings.benchmarks
    navs = nav_series(app, portfolio_id, start, end)
    known = [(d, v) for d, v in navs if v is not None]
    missing_days = [d for d, v in navs if v is None]
    flows = external_flows(app, portfolio_id, end)
    flow_by_day: dict[date, Decimal] = {}
    for d, amt, _ in flows:
        if amt is not None and start <= d <= end:
            k = cal.session_on_or_after(d)
            flow_by_day[k] = flow_by_day.get(k, ZERO) + amt
    first_nav_day = known[0][0] if known else None
    twr, idx = time_weighted_return(known, {d: a for d, a in flow_by_day.items() if first_nav_day and d > first_nav_day})
    dd = max_drawdown(idx)
    mwr_flows = [(d, -a) for d, a in flow_by_day.items() if first_nav_day and d > first_nav_day]
    if known:
        mwr_flows = [(known[0][0], -known[0][1])] + mwr_flows + [(known[-1][0], known[-1][1])]
    mwr = xirr(mwr_flows) if known else None
    end_view = portfolio_view(app, portfolio_id, end)
    trades = [t for st in end_view.account_states.values() for t in st.trades if start <= t[0] <= end]
    gross = sum((t[4] for t in trades), ZERO)
    avg_nav = sum((v for _, v in known), ZERO) / len(known) if known else None
    costs = {r["category"]: Decimal(str(r["s"])) for r in all_rows(
        app.conn, "SELECT category, COALESCE(SUM(CAST(amount_usd AS REAL)),0) AS s FROM cost_record WHERE substr(occurred_at,1,10) "
                  "BETWEEN ? AND ? GROUP BY category", (start.isoformat(), end.isoformat()))}
    unknown_cost = one(app.conn, "SELECT COUNT(*) AS n FROM cost_record WHERE amount_usd IS NULL")["n"]
    unreal = [h.unrealized_gain for h in end_view.holdings]
    unrealized = None if any(u is None for u in unreal) else sum(unreal, ZERO)
    net_contrib = sum((a for _, a, _ in flows if a is not None), ZERO)
    recon = None
    if end_view.nav is not None and unrealized is not None and end_view.realized_gain is not None \
            and all(a is not None for _, a, _ in flows):
        recon_value = net_contrib + end_view.realized_gain + unrealized + end_view.dividends + end_view.interest
        recon = {"nav": end_view.nav, "contributions+pnl+income": recon_value,
                 "difference_is_standalone_fees": end_view.nav - recon_value,
                 "note": "trade fees are inside cost basis/proceeds; the difference should equal minus standalone FEE events"}
    bench = {}
    for sym in benchmarks:
        try:
            b = contribution_matched(app, portfolio_id, sym, start, end)
        except KeyError as exc:
            bench[sym] = {"error": str(exc)}
            continue
        bflows = {d: a for d, a in flow_by_day.items() if first_nav_day and d > first_nav_day}
        btwr, bidx = time_weighted_return([(d, v) for d, v in b.values if v > 0], bflows)
        bench[sym] = {"end_value": b.values[-1][1] if b.values else None, "twr": btwr,
                      "start_value": b.values[0][1] if b.values else None, "mode": b.mode,
                      "replayed_from": b.replay_start, "warnings": b.warnings,
                      "max_drawdown": max_drawdown(bidx)[0], "unapplied_flows": b.unapplied,
                      "assumptions": "close of first session on/after flow date; no fees; dividends reinvested at ex-date close"}
        for d, v in b.values[-1:]:
            insert(app.conn, "benchmark_snapshot", {"id": new_id("bm"), "portfolio_id": portfolio_id, "benchmark_symbol": sym,
                                                    "as_of_date": d.isoformat(), "units": str(b.units), "value": str(v),
                                                    "method_json": to_json({"start": start, "end": end, "mode": b.mode,
                                                                            "replayed_from": b.replay_start}),
                                                    "created_at": app.now_iso()}, or_ignore=True)
    return {
        "label": end_view.kind, "pre_tax": True, "start": start, "end": end, "sessions": len(navs),
        "nav_start": known[0][1] if known else None, "nav_end": known[-1][1] if known else None,
        "missing_nav_days": missing_days, "twr": twr,
        "mwr": {"rate": mwr.rate, "status": mwr.status, "note": mwr.note} if mwr else None,
        "max_drawdown": dd[0], "drawdown_window": (dd[1], dd[2]),
        "realized_gain": end_view.realized_gain, "unrealized_gain": unrealized, "dividends": end_view.dividends,
        "fees": end_view.fees, "turnover": (gross / avg_nav) if avg_nav else None, "trade_count": len(trades),
        "research_costs": costs, "unknown_cost_records": unknown_cost, "nav_reconciliation": recon, "benchmarks": bench,
        "primary_benchmark": benchmarks[0] if benchmarks else None,
        "caveat": "A short live record cannot establish skill. No alpha claim is made.",
    }


def process_metrics(app: App) -> dict:
    fact_claims = one(app.conn, "SELECT COUNT(*) AS n, SUM(CASE WHEN verification <> 'VERIFIED' THEN 1 ELSE 0 END) AS bad "
                                "FROM claim WHERE claim_type='FACT'")
    stale_codes = ("MISSING_PRICE", "STALE_PRICE", "FILINGS_NOT_CHECKED", "FINANCIALS_OVERDUE", "DATA_REFRESH_FAILED",
                   "NEW_FINANCIALS_SINCE_VALUATION", "VALUATION_STALE")
    recs = all_rows(app.conn, "SELECT reason_codes_json, action, previous_action FROM recommendation")
    blocked = sum(1 for r in recs if r["action"] == "REVIEW" and any(c in r["reason_codes_json"] for c in stale_codes))
    revisions = sum(1 for r in recs if r["previous_action"] and r["previous_action"] != r["action"])
    lag = all_rows(app.conn, "SELECT (julianday(detected_at) - julianday(public_at)) * 24 AS h FROM detected_event "
                             "WHERE event_type='NEW_FILING' AND public_at IS NOT NULL")
    hours = sorted(r["h"] for r in lag if r["h"] is not None)
    ms = {r["state"]: r["n"] for r in all_rows(app.conn, "SELECT state, COUNT(*) AS n FROM condition_assessment "
                                                         "WHERE subject_type='MILESTONE' GROUP BY state")}
    llm = one(app.conn, "SELECT COUNT(*) AS n, SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) AS ok FROM llm_call")
    return {
        "fact_claims": fact_claims["n"], "unsupported_claim_rate": (fact_claims["bad"] or 0) / fact_claims["n"] if fact_claims["n"] else None,
        "recommendations": len(recs), "stale_input_decisions_blocked": blocked, "recommendation_revisions": revisions,
        "filing_detection_lag_hours_median": hours[len(hours) // 2] if hours else None, "milestone_assessments": ms,
        "llm_calls": llm["n"], "llm_calls_valid": llm["ok"] or 0,
        "llm_incremental_usefulness": "not yet measurable: requires a prospective comparison of LLM-assisted vs deterministic-only reviews",
    }
