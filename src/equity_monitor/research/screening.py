"""Deterministic screening: exclusions, metrics, peer-relative scores -> research shortlist.

A screen score ranks *what to research next*. It is not an expected return, not a valuation
and not a recommendation. Metric definitions (all point-in-time as of the run):

  revenue_cagr_3y        (Rev_FY0 / Rev_FY-3)^(1/3) - 1                         higher better
  op_margin_avg_3y       mean(OperatingIncome / Revenue), last 3 FY               higher better
  op_margin_trend        margin_FY0 - margin_FY-2                                 higher better
  fcf_margin_avg_3y      mean((CFO - Capex) / Revenue), last 3 FY                 higher better
  fcf_positive_years     count of FY with CFO - Capex > 0 (last 5)                higher better
  roic                   EBIT*(1-21%) / (Debt + Equity - Cash); None if denom<=0  higher better
  net_debt_to_ebitda     (Debt - Cash) / (EBIT + D&A); None if EBITDA<=0          lower better
  interest_coverage      EBIT / InterestExpense; None if interest missing or 0    higher better
  share_cagr_3y          diluted weighted shares CAGR                             lower better
  sbc_to_revenue         SBC / Revenue (latest FY)                                lower better
  net_buyback_yield      (Buybacks - SBC) / MarketCap                             higher better
  fcf_yield              mean FCF (3y) / MarketCap; None if MarketCap<=0           higher better
  ev_to_ebit             EV / EBIT (latest FY); None if EBIT<=0 or EV<=0          lower better

Invalid denominators produce ``None`` (with a reason) and contribute nothing to scores, so a
negative earnings base can never look "cheap".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..app import App
from ..data.prices import price_on_or_before
from ..db.core import insert, one
from ..util import dstr, new_id, to_json
from .fundamentals import FactView

ZERO = Decimal(0)
QUALITY = {"op_margin_avg_3y": 1, "op_margin_trend": 1, "fcf_margin_avg_3y": 1, "fcf_positive_years": 1, "roic": 1,
           "net_debt_to_ebitda": -1, "interest_coverage": 1, "share_cagr_3y": -1, "sbc_to_revenue": -1,
           "revenue_cagr_3y": 1}
VALUE = {"fcf_yield": 1, "ev_to_ebit": -1, "net_buyback_yield": 1}


@dataclass
class ScreenInput:
    security_id: str
    symbol: str
    sic: str | None
    peer_group: str | None
    fv: FactView
    price: Decimal | None
    security_type: str = "COMMON"


@dataclass
class ScreenRow:
    security_id: str
    symbol: str
    eligible: bool
    exclusion_reason: str | None
    metrics: dict[str, Decimal | None]
    notes: dict[str, str] = field(default_factory=dict)
    peer_group: str = "ALL"
    quality_score: Decimal | None = None
    value_score: Decimal | None = None
    score: Decimal | None = None
    completeness: Decimal = ZERO
    rank: int | None = None


def _ann(fv: FactView, concept: str) -> dict[date, Decimal]:
    return {f.end: f.value for f in fv.annual(concept) if f.value is not None}


def compute_metrics(si: ScreenInput) -> tuple[dict[str, Decimal | None], dict[str, str]]:
    fv = si.fv
    m: dict[str, Decimal | None] = {k: None for k in list(QUALITY) + list(VALUE)}
    notes: dict[str, str] = {}
    rev, oi = _ann(fv, "revenue"), _ann(fv, "operating_income")
    cfo, capex = _ann(fv, "cfo"), _ann(fv, "capex")
    ends = sorted(rev)
    if len(ends) >= 4 and rev[ends[-4]] > 0 and rev[ends[-1]] > 0:
        m["revenue_cagr_3y"] = Decimal(str((float(rev[ends[-1]]) / float(rev[ends[-4]])) ** (1 / 3) - 1))
    margins = [(e, oi[e] / rev[e]) for e in ends if e in oi and rev[e] > 0]
    if margins:
        last3 = margins[-3:]
        m["op_margin_avg_3y"] = sum((x for _, x in last3), ZERO) / len(last3)
        if len(last3) == 3:
            m["op_margin_trend"] = last3[-1][1] - last3[0][1]
    fcf = {e: cfo[e] - capex[e] for e in cfo if e in capex}
    fcf_m = [fcf[e] / rev[e] for e in sorted(fcf) if e in rev and rev[e] > 0][-3:]
    if fcf_m:
        m["fcf_margin_avg_3y"] = sum(fcf_m, ZERO) / len(fcf_m)
    if fcf:
        m["fcf_positive_years"] = Decimal(sum(1 for e in sorted(fcf)[-5:] if fcf[e] > 0))
    else:
        notes["fcf"] = "operating cash flow or capex missing"
    ebit = oi.get(ends[-1]) if ends else None
    cash_f, sti_f = fv.instant("cash"), fv.instant("short_term_investments")
    cash = (cash_f.value if cash_f and cash_f.value is not None else None)
    if cash is not None and sti_f and sti_f.value is not None:
        cash += sti_f.value
    ltd, cd, eq = fv.instant("long_term_debt"), fv.instant("current_debt"), fv.instant("total_equity")
    debt = None
    if ltd or cd:
        debt = (ltd.value if ltd and ltd.value else ZERO) + (cd.value if cd and cd.value else ZERO)
    if ebit is not None and debt is not None and eq and eq.value is not None and cash is not None:
        ic = debt + eq.value - cash
        if ic > 0:
            m["roic"] = ebit * Decimal("0.79") / ic
        else:
            notes["roic"] = "invested capital <= 0: not meaningful"
    dna = _ann(fv, "dna")
    if ebit is not None and debt is not None and cash is not None and ends and ends[-1] in dna:
        ebitda = ebit + dna[ends[-1]]
        if ebitda > 0:
            m["net_debt_to_ebitda"] = (debt - cash) / ebitda
        else:
            notes["net_debt_to_ebitda"] = "EBITDA <= 0: not meaningful"
    intexp = _ann(fv, "interest_expense")
    if ebit is not None and ends and ends[-1] in intexp:
        if intexp[ends[-1]] > 0:
            m["interest_coverage"] = ebit / intexp[ends[-1]]
        else:
            notes["interest_coverage"] = "no interest expense reported"
    sh = _ann(fv, "shares_diluted_weighted")
    she = sorted(sh)
    if len(she) >= 4 and sh[she[-4]] > 0:
        m["share_cagr_3y"] = Decimal(str((float(sh[she[-1]]) / float(sh[she[-4]])) ** (1 / 3) - 1))
    sbc = _ann(fv, "sbc")
    if ends and ends[-1] in sbc and rev[ends[-1]] > 0:
        m["sbc_to_revenue"] = sbc[ends[-1]] / rev[ends[-1]]
    # valuation metrics need a price and a share count
    shares = None
    q = fv.quarters("shares_diluted_weighted")
    if q and q[-1].value:
        shares = q[-1].value
    elif she:
        shares = sh[she[-1]]
    if si.price is None or shares is None:
        notes["valuation"] = "price or share count missing"
    else:
        mcap = si.price * shares
        if mcap <= 0:
            notes["valuation"] = "market cap <= 0"
        else:
            if fcf:
                last = [fcf[e] for e in sorted(fcf)[-3:]]
                m["fcf_yield"] = (sum(last, ZERO) / len(last)) / mcap
            buy = _ann(fv, "buybacks")
            if ends and ends[-1] in buy:
                m["net_buyback_yield"] = (buy[ends[-1]] - sbc.get(ends[-1], ZERO)) / mcap
            if ebit is not None and debt is not None and cash is not None:
                ev = mcap + debt - cash
                if ebit > 0 and ev > 0:
                    m["ev_to_ebit"] = ev / ebit
                else:
                    notes["ev_to_ebit"] = "EBIT or EV <= 0: not meaningful"
    return m, notes


def exclusion(app: App, si: ScreenInput) -> str | None:
    p = app.policy.screening
    if si.security_type in ("ETF", "FUND"):
        return "ETF_OR_FUND"
    if si.sic:
        s = int(si.sic)
        for lo, hi, label in p.excluded_sic_ranges:
            if lo <= s <= hi:
                return label
        if s in p.biotech_sic:
            revs = si.fv.annual("revenue")
            if not revs or revs[-1].value is None or revs[-1].value < p.biotech_min_revenue_usd:
                return "PRE_REVENUE_BIOTECH"
    else:
        return "UNKNOWN_INDUSTRY"
    fy_rev = [f for f in si.fv.annual("revenue") if f.value is not None]
    fy_cfo = [f for f in si.fv.annual("cfo") if f.value is not None]
    if len(fy_rev) < p.min_fiscal_years or len(fy_cfo) < p.min_fiscal_years:
        return "INSUFFICIENT_HISTORY"
    return None


def _percentile_scores(rows: list[ScreenRow], metric: str, direction: int) -> dict[str, Decimal]:
    vals = [(r.security_id, r.metrics[metric]) for r in rows if r.metrics.get(metric) is not None]
    if len(vals) < 2:
        return {}
    ordered = sorted(vals, key=lambda x: (x[1] * direction, x[0]))
    n = len(ordered)
    return {sid: Decimal(i) / Decimal(n - 1) for i, (sid, _) in enumerate(ordered)}


def score(app: App, inputs: list[ScreenInput]) -> list[ScreenRow]:
    rows = []
    for si in inputs:
        reason = exclusion(app, si)
        metrics, notes = compute_metrics(si)
        rows.append(ScreenRow(si.security_id, si.symbol, reason is None, reason, metrics, notes,
                              peer_group=si.peer_group or "ALL"))
    eligible = [r for r in rows if r.eligible]
    groups: dict[str, list[ScreenRow]] = {}
    for r in eligible:
        groups.setdefault(r.peer_group, []).append(r)
    min_peers = app.policy.screening.min_peer_group_size
    for g, members in list(groups.items()):
        if len(members) < min_peers:
            for r in members:
                r.peer_group = "ALL"   # too few peers: compare against the whole eligible set (flagged)
    pools: dict[str, list[ScreenRow]] = {}
    for r in eligible:
        pools.setdefault(r.peer_group, []).append(r)
    for pool in pools.values():
        pct: dict[str, dict[str, Decimal]] = {k: _percentile_scores(pool, k, d) for k, d in {**QUALITY, **VALUE}.items()}
        for r in pool:
            q = [pct[k][r.security_id] for k in QUALITY if r.security_id in pct[k]]
            v = [pct[k][r.security_id] for k in VALUE if r.security_id in pct[k]]
            r.completeness = Decimal(len(q) + len(v)) / Decimal(len(QUALITY) + len(VALUE))
            r.quality_score = sum(q, ZERO) / len(q) if q else None
            r.value_score = sum(v, ZERO) / len(v) if v else None
            if r.completeness < app.policy.screening.min_completeness or r.quality_score is None or r.value_score is None:
                r.score = None
                r.notes["score"] = f"insufficient data (completeness {r.completeness:.0%})"
            else:
                r.score = (r.quality_score + r.value_score) / 2
    ranked = sorted([r for r in eligible if r.score is not None], key=lambda r: (-r.score, r.symbol))
    for i, r in enumerate(ranked, 1):
        r.rank = i
    return rows


def run_screen(app: App, security_ids: list[str], as_of: datetime, label: str = "CURRENT") -> tuple[str, list[ScreenRow]]:
    """Screen the given securities as of ``as_of``. ``label`` must be ENGINEERING_ONLY for historical
    runs without point-in-time universe membership (see DATA_COVERAGE.md)."""
    inputs = []
    for sid in security_ids:
        r = one(app.conn, "SELECT s.id, s.symbol, s.security_type, i.id AS iid, i.sic, i.industry_group FROM security s "
                          "LEFT JOIN issuer i ON i.id=s.issuer_id WHERE s.id=?", (sid,))
        if r["iid"] is None:
            continue
        px = price_on_or_before(app, sid, as_of.date())
        inputs.append(ScreenInput(sid, r["symbol"], r["sic"], r["industry_group"], FactView(app, r["iid"], as_of),
                                  px[1] if px else None, r["security_type"]))
    rows = score(app, inputs)
    run_id = new_id("scr")
    insert(app.conn, "screening_run", {"id": run_id, "as_of": as_of.isoformat(), "universe_snapshot_id": None,
                                       "config_hash": app.policy.content_hash(), "label": label,
                                       "created_at": app.now_iso()})
    for r in rows:
        insert(app.conn, "screening_result", {
            "run_id": run_id, "security_id": r.security_id, "eligible": int(r.eligible),
            "exclusion_reason": r.exclusion_reason, "metrics_json": to_json({"metrics": r.metrics, "notes": r.notes,
                                                                            "quality": r.quality_score, "value": r.value_score}),
            "score": dstr(r.score), "completeness": dstr(r.completeness), "peer_group": r.peer_group, "rank": r.rank,
        })
    return run_id, rows
