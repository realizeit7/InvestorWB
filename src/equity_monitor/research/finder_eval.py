"""Prospective evaluation of finder cohorts (POLICY.md §13.7). HYPOTHETICAL — nothing here is traded.

Each comparison arm of each finder run is a FROZEN cohort (``finder_cohort``) with an information time: when every
input to that selection existed (A/B/C: when the run was recorded; D: when its judgments were frozen). A cohort is
evaluated as an executable equal-weight buy-and-hold:

- entry at the OPENING price of the first session that opens after the information time (a Sunday shortlist is
  bought Monday at the open, never at Friday's close); exit at the open ``h`` sessions later;
- SPY over exactly the same open-to-open interval; dividends and splits from recorded corporate actions;
- ``finder.evaluation.cost_bps_per_side`` charged on every buy and sell of a member (SPY is charged nothing, which
  is conservative for the finder);
- a member without an opening price at entry or exit (delisted, acquired, halted, data gap) is reported as MISSING;
  a cohort with any missing member is INCOMPLETE and excluded from the statistics — the priced subset is shown
  separately and never presented as the cohort's result.

Statistics are per protocol (identical finder rules) and arm. Overlapping weekly cohorts and repeated companies are
correlated: uncertainty uses a moving-block bootstrap over the cohort sequence (block = horizon in weeks), counts are
never treated as independent bets, and the verdict stays INSUFFICIENT_DATA / INCONCLUSIVE unless the predeclared gate
and every condition hold. Descriptive close-to-close price studies are not produced here.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.prices import actions_for, bar_on, price_series
from ..data.securities import find_security
from ..db.core import all_rows
from ..util import parse_utc

ARM_BASELINE = {"B_RAW_GAP": "A_QUALITY_VALUE", "C_CONSERVATIVE_GAP": "A_QUALITY_VALUE", "D_LLM_RULE": "B_RAW_GAP"}
LABEL = ("HYPOTHETICAL executable cohort study: equal weight, bought at the first opening price after the information "
         "existed, sold at the open h sessions later, costs charged; SPY over the same interval. Not traded; not "
         "evidence of an edge unless the predeclared gate is met.")


def entry_session(info_time: datetime) -> date:
    d = cal.session_on_or_after(cal.ny_date(info_time))
    if cal.session_open_utc(d) <= info_time:
        d = cal.next_session(d)
    return d


def add_sessions(d: date, n: int) -> date:
    for _ in range(n):
        d = cal.next_session(d)
    return d


def _open(app: App, sid: str, d: date) -> Decimal | None:
    b = bar_on(app, sid, d)
    return Decimal(b["open"]) if b and b.get("open") not in (None, "") else None


def open_to_open(app: App, sid: str | None, entry: date, exit_: date) -> tuple[float | None, str | None, list]:
    """Total return from the entry open to the exit open, with the daily path (close-based) for drawdowns."""
    if sid is None:
        return None, "security unknown", []
    p0, p1 = _open(app, sid, entry), _open(app, sid, exit_)
    if p0 is None or p0 <= 0:
        return None, "no opening price at entry", []
    if p1 is None:
        return None, "no opening price at exit (delisted, acquired, halted or data gap)", []
    closes = price_series(app, sid, entry, exit_)
    acts: dict[date, list[dict]] = {}
    for a in actions_for(app, sid, entry + timedelta(days=1), exit_):
        acts.setdefault(date.fromisoformat(a["ex_date"]), []).append(a)
    units, cash, path = Decimal(1), Decimal(0), []
    for d in sorted(set(closes) | set(acts) | {exit_}):
        for a in acts.get(d, []):
            if a["action_type"] == "SPLIT":
                units *= Decimal(a["ratio_num"]) / Decimal(a["ratio_den"])
        for a in acts.get(d, []):
            if a["action_type"] == "CASH_DIVIDEND":
                if d < exit_ and closes.get(d):
                    units += units * Decimal(a["cash_amount"]) / closes[d]   # reinvested at the ex-date close
                elif d == exit_:
                    cash += units * Decimal(a["cash_amount"])               # received; not reinvested
        if d < exit_ and d in closes:
            path.append((d, float(units * closes[d] / p0)))
    final = float((units * p1 + cash) / p0)
    path.append((exit_, final))
    return final - 1, None, path


def _drawdown(path: list[tuple[date, float]]) -> float | None:
    if not path:
        return None
    peak, worst = 1.0, 0.0
    for _d, v in path:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def _portfolio_path(paths: list[list]) -> list:
    """Equal-weight buy-and-hold path (value-weighted drift), forward-filling missing closes."""
    dates = sorted({d for p in paths for d, _ in p})
    if not dates:
        return []
    last = [1.0] * len(paths)
    lookup = [dict(p) for p in paths]
    out = []
    for d in dates:
        for i, lk in enumerate(lookup):
            if d in lk:
                last[i] = lk[d]
        out.append((d, sum(last) / len(last)))
    return out


def _beta(app: App, sid: str, spy: str, entry: date, n: int) -> float | None:
    start = entry - timedelta(days=int(n * 1.6))
    a, b = price_series(app, sid, start, entry - timedelta(days=1)), price_series(app, spy, start, entry - timedelta(days=1))
    ds = sorted(set(a) & set(b))[-(n + 1):]
    if len(ds) < int(n * 0.8):
        return None
    ra = [float(a[ds[i]] / a[ds[i - 1]] - 1) for i in range(1, len(ds))]
    rb = [float(b[ds[i]] / b[ds[i - 1]] - 1) for i in range(1, len(ds))]
    vb = statistics.pvariance(rb)
    if vb == 0:
        return None
    mb, ma = statistics.fmean(rb), statistics.fmean(ra)
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / len(ra) / vb


def cohorts(app: App) -> list[dict]:
    """Frozen cohorts plus, for runs made before cohorts existed, a 'legacy' arm-B cohort from the recorded shortlist
    (its information time is the run's recording time; it is reported separately and never counts toward the gate)."""
    out = [dict(r) | {"members": json.loads(r["members_json"]), "excluded": json.loads(r["excluded_json"])}
           for r in all_rows(app.conn, "SELECT * FROM finder_cohort ORDER BY info_time, rowid")]
    have = {c["run_id"] for c in out}
    for run in all_rows(app.conn, "SELECT id, created_at FROM finder_run ORDER BY created_at"):
        if run["id"] in have:
            continue
        mem = [{"symbol": r["symbol"], "security_id": r["security_id"], "rank": r["rank"]} for r in all_rows(
            app.conn, "SELECT symbol, security_id, rank FROM finder_candidate WHERE run_id=? AND stage='SHORTLIST' "
                      "ORDER BY rank", (run["id"],))]
        out.append({"id": f"legacy:{run['id']}", "run_id": run["id"], "arm": "B_RAW_GAP", "cohort_no": 1,
                    "protocol_hash": "legacy-unfrozen", "info_time": run["created_at"], "members": mem, "excluded": []})
    return out


def evaluate_cohort(app: App, c: dict, h: int, today: date, spy: str | None) -> dict:
    ev = app.policy.finder.evaluation
    cost = float(ev.cost_bps_per_side) / 10000
    entry = entry_session(parse_utc(c["info_time"]))
    exit_ = add_sessions(entry, h)
    row = {"cohort_id": c["id"], "run_id": c["run_id"], "arm": c["arm"], "cohort_no": c["cohort_no"],
           "protocol_hash": c["protocol_hash"], "info_time": c["info_time"], "entry": entry.isoformat(),
           "exit": exit_.isoformat(), "horizon_sessions": h, "members": len(c["members"])}
    if exit_ > today:
        return row | {"status": "NOT_MATURED"}
    if not c["members"]:
        return row | {"status": "EMPTY", "note": "the rule selected nothing (cash); excluded from statistics"}
    spy_r, spy_why, spy_path = open_to_open(app, spy, entry, exit_)
    if spy_r is None:
        return row | {"status": "SPY_MISSING", "note": f"SPY: {spy_why}"}
    res, missing, paths = [], [], []
    for m in c["members"]:
        r, why, path = open_to_open(app, m.get("security_id"), entry, exit_)
        if r is None:
            missing.append({"symbol": m["symbol"], "reason": why})
            continue
        net = (1 - cost) * (1 + r) * (1 - cost) - 1
        res.append({"symbol": m["symbol"], "security_id": m.get("security_id"), "gross": r, "net": net})
        paths.append(path)
    row |= {"spy_return": spy_r, "priced": len(res), "missing": missing,
            "coverage": len(res) / len(c["members"])}
    if missing:
        sub = statistics.fmean(x["net"] for x in res) if res else None
        return row | {"status": "INCOMPLETE", "priced_subset_mean_net": sub,
                      "note": "not the cohort's result: missing members' outcomes are unknown"}
    n = len(res)
    net = statistics.fmean(x["net"] for x in res)
    contrib = {x["security_id"] or x["symbol"]: (x["net"] - spy_r) / n for x in res}
    cand = {r["symbol"]: dict(r) for r in all_rows(app.conn, "SELECT symbol, sector, market_cap_usd FROM finder_candidate "
                                                             "WHERE run_id=? AND stage='DEEP'", (c["run_id"],))}
    sectors: dict[str, int] = {}
    for x in res:
        k = (cand.get(x["symbol"]) or {}).get("sector") or "unknown"
        sectors[k] = sectors.get(k, 0) + 1
    caps = [float(cand[x["symbol"]]["market_cap_usd"]) for x in res
            if cand.get(x["symbol"]) and cand[x["symbol"]]["market_cap_usd"]]
    betas = [b for b in (_beta(app, x["security_id"], spy, entry, ev.beta_lookback_sessions) for x in res
                         if x["security_id"]) if b is not None]
    pos = sorted((v for v in contrib.values() if v > 0), reverse=True)
    return row | {"status": "COMPLETE", "net_return": net, "gross_return": statistics.fmean(x["gross"] for x in res),
                  "excess_net_vs_spy": net - spy_r, "contributions": contrib,
                  "top3_share_of_positive_excess": (sum(pos[:3]) / sum(pos)) if pos else None,
                  "max_drawdown": _drawdown(_portfolio_path(paths)), "spy_max_drawdown": _drawdown(spy_path),
                  "sectors": sectors, "median_market_cap_usd": statistics.median(caps) if caps else None,
                  "mean_pre_entry_beta_vs_spy": statistics.fmean(betas) if betas else None,
                  "beta_coverage": len(betas) / n}


def _block_bootstrap_ci(xs: list[float], block: int, samples: int, level: float) -> tuple[float, float] | None:
    if len(xs) < 2:
        return None
    rng = random.Random(0)                                   # deterministic: same data, same interval
    n, block = len(xs), max(1, min(block, len(xs)))
    starts = list(range(n - block + 1))
    means = []
    for _ in range(samples):
        draw: list[float] = []
        while len(draw) < n:
            s = rng.choice(starts)
            draw.extend(xs[s:s + block])
        means.append(statistics.fmean(draw[:n]))
    means.sort()
    lo, hi = (1 - level) / 2, 1 - (1 - level) / 2
    return means[int(lo * (samples - 1))], means[int(math.ceil(hi * (samples - 1)))]


def _months(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    ds = sorted(date.fromisoformat(r["entry"]) for r in rows)
    return (ds[-1] - ds[0]).days / 30.44


def summarize(app: App, rows: list[dict], arm: str, h: int, baseline_rows: list[dict] | None) -> dict:
    ev = app.policy.finder.evaluation
    done = sorted([r for r in rows if r["status"] == "COMPLETE"], key=lambda r: r["entry"])
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in
              ("COMPLETE", "INCOMPLETE", "EMPTY", "SPY_MISSING", "NOT_MATURED")}
    out: dict = {"arm": arm, "horizon_sessions": h, "cohorts": counts,
                 "missing_member_outcomes": sum(len(r.get("missing", [])) for r in rows)}
    if not done:
        return out | {"verdict": "INSUFFICIENT_DATA", "why": "no complete matured cohort yet"}
    xs = [r["excess_net_vs_spy"] for r in done]
    block = max(1, math.ceil(h / 5))
    ci = _block_bootstrap_ci(xs, block, ev.bootstrap_samples, float(ev.ci_level))
    issuer: dict[str, float] = {}
    for r in done:
        for k, v in r["contributions"].items():
            issuer[k] = issuer.get(k, 0.0) + v
    pos = sorted((v for v in issuer.values() if v > 0), reverse=True)
    top5 = (sum(pos[:5]) / sum(pos)) if pos else None
    dds = [r["max_drawdown"] for r in done if r["max_drawdown"] is not None]
    out |= {"complete_cohorts": len(done), "span_months": round(_months(done), 1),
            "mean_net_excess_vs_spy": statistics.fmean(xs), "median_net_excess_vs_spy": statistics.median(xs),
            "share_of_cohorts_beating_spy": sum(1 for x in xs if x > 0) / len(xs),
            "mean_net_return": statistics.fmean(r["net_return"] for r in done),
            "mean_spy_return": statistics.fmean(r["spy_return"] for r in done),
            "mean_max_drawdown": statistics.fmean(dds) if dds else None,
            "ci": {"level": float(ev.ci_level), "method": f"moving-block bootstrap over the cohort sequence, block "
                   f"{block} cohorts", "mean_net_excess": ci},
            "distinct_issuers": len(issuer), "top5_issuer_share_of_positive_excess": top5}
    improvement = None
    if baseline_rows is not None:
        base = {r["run_id"]: r for r in baseline_rows if r["status"] == "COMPLETE"}
        pairs = [(r["excess_net_vs_spy"] - base[r["run_id"]]["excess_net_vs_spy"]) for r in done if r["run_id"] in base]
        if pairs:
            improvement = {"vs": ARM_BASELINE[arm], "paired_cohorts": len(pairs), "mean": statistics.fmean(pairs),
                           "ci": _block_bootstrap_ci(pairs, block, ev.bootstrap_samples, float(ev.ci_level))}
        out["improvement_over_baseline"] = improvement or {"vs": ARM_BASELINE[arm], "paired_cohorts": 0}
    # verdict (POLICY.md §13.7): minimum observations first, then every condition, then uncertainty
    gate_cohorts = ev.gate_min_primary_cohorts if h == ev.primary_horizon_sessions else 0
    if out["span_months"] < ev.gate_min_months or len(done) < gate_cohorts:
        return out | {"verdict": "INSUFFICIENT_DATA",
                      "why": f"{len(done)} complete cohorts over {out['span_months']} months; the gate needs "
                             f"{ev.gate_min_primary_cohorts} primary-horizon cohorts over {ev.gate_min_months} months"}
    if out["mean_net_excess_vs_spy"] <= 0:
        return out | {"verdict": "NOT_SUPPORTED", "why": "mean net excess vs SPY is not positive"}
    if baseline_rows is not None and (improvement is None or improvement["mean"] <= 0):
        return out | {"verdict": "NOT_SUPPORTED", "why": f"does not improve on its baseline {ARM_BASELINE[arm]}"}
    if top5 is not None and top5 > float(ev.max_top5_issuer_share):
        return out | {"verdict": "INCONCLUSIVE", "why": f"dominated by a handful of companies (top 5 = {top5:.0%})"}
    wide = ci is None or ci[0] <= 0 or (improvement is not None and (improvement["ci"] is None or improvement["ci"][0] <= 0))
    if wide:
        return out | {"verdict": "INCONCLUSIVE", "why": "uncertainty interval includes zero"}
    return out | {"verdict": "PROMISING", "why": "net excess positive, improves on its baseline, not concentrated, "
                                                "interval excludes zero (still not proof of skill)"}


def evaluate(app: App, *, include_rows: bool = False) -> dict:
    ev = app.policy.finder.evaluation
    today = cal.latest_completed_session(app.now())
    spy = find_security(app.conn, "SPY")
    all_c = cohorts(app)
    groups: dict[str, list[dict]] = {}
    for c in all_c:
        groups.setdefault(c["protocol_hash"], []).append(c)
    from .finder import protocol_hash
    current = protocol_hash(app)
    report = {"label": LABEL, "today": today.isoformat(), "current_protocol": current,
              "primary_horizon_sessions": ev.primary_horizon_sessions, "protocols": []}
    for ph, cs in groups.items():
        first_d: dict[str, dict] = {}
        for c in cs:                                         # arm D statistics use the FIRST freeze of each run
            if c["arm"] == "D_LLM_RULE" and c["run_id"] not in first_d:
                first_d[c["run_id"]] = c
        later_d = [c for c in cs if c["arm"] == "D_LLM_RULE" and first_d.get(c["run_id"]) is not c]
        use = [c for c in cs if c["arm"] != "D_LLM_RULE"] + list(first_d.values())
        per = {"protocol_hash": ph, "is_current": ph == current, "legacy": ph == "legacy-unfrozen",
               "runs": len({c["run_id"] for c in cs}), "later_llm_refreezes_not_in_statistics": len(later_d),
               "arms": []}
        for h in ev.horizons_sessions:
            rows = {arm: [evaluate_cohort(app, c, h, today, spy) for c in use if c["arm"] == arm]
                    for arm in sorted({c["arm"] for c in use})}
            for arm, rs in rows.items():
                base = rows.get(ARM_BASELINE.get(arm, ""), None) if arm in ARM_BASELINE else None
                if arm in ARM_BASELINE and base is None:
                    base = []
                summ = summarize(app, rs, arm, h, base)
                summ["primary"] = h == ev.primary_horizon_sessions
                if per["legacy"]:
                    summ["verdict"], summ["why"] = "NOT_EVALUABLE", "legacy run without frozen cohorts"
                if include_rows:
                    summ["rows"] = rs
                per["arms"].append(summ)
        report["protocols"].append(per)
    return report
