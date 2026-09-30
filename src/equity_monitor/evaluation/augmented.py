"""Fundamental-only baseline vs market-context-augmented system (prospective).

Both variants share the same long-term engine and limits; the baseline ignores current-conditions pauses.
Stored per recommendation (``baseline_eligibility`` vs ``purchase_eligibility``) and per allocation
proposal (``baseline`` block), so the comparison is computed from records made at decision time.

Reported: pauses and their durations, cash withheld relative to the baseline (cash drag), subsequent
returns of paused names vs SPY and the sector benchmark (association only), source quality, alert
usefulness (owner feedback), turnover implications and costs. With a short record the verdict is
"insufficient evidence" — more information is not assumed to improve results.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.securities import find_security
from ..db.core import all_rows, one
from ..market.metrics import tr_series
from ..util import from_json, parse_utc

MIN_PAUSES_FOR_READOUT = 20
MIN_DAYS_AFTER_PAUSE = 60


def _fwd(app: App, sid: str | None, start: date, end: date) -> float | None:
    if sid is None:
        return None
    s = [x for x in tr_series(app, sid, end, (end - start).days + 10) if x[0] >= start]
    return (s[-1][1] / s[0][1] - 1) if len(s) > 1 else None


def compare(app: App, portfolio_id: str) -> dict:
    recs = all_rows(app.conn, "SELECT r.*, s.symbol FROM recommendation r JOIN security s ON s.id=r.security_id WHERE "
                              "r.portfolio_id=? AND r.purchase_eligibility IS NOT NULL ORDER BY r.as_of", (portfolio_id,))
    diverged = [r for r in recs if r["purchase_eligibility"] != r["baseline_eligibility"]]
    session = cal.latest_completed_session(app.now())
    spy = find_security(app.conn, "SPY")
    outcomes = []
    for r in diverged:
        if r["purchase_eligibility"] != "PAUSED" or r["action"] != "ADD":
            continue
        start = parse_utc(r["as_of"]).date()
        if (session - start).days < MIN_DAYS_AFTER_PAUSE:
            continue
        p = from_json(r["payload_json"])
        etf = None
        if r["exposure_version_id"]:
            prof = from_json(one(app.conn, "SELECT content_json FROM exposure_profile_version WHERE id=?",
                                 (r["exposure_version_id"],))["content_json"])
            etf = find_security(app.conn, prof.get("sector_benchmark") or "")
        stock = _fwd(app, r["security_id"], start, session)
        outcomes.append({"symbol": r["symbol"], "paused_on": start, "codes": [x["code"] for x in p["current_conditions"]["pauses"]],
                         "return_since": stock, "spy_since": _fwd(app, spy, start, session),
                         "sector_since": _fwd(app, etf, start, session)})
    allocs = [from_json(a["payload_json"]) for a in all_rows(app.conn, "SELECT payload_json FROM allocation_proposal WHERE portfolio_id=? "
                                                                       "ORDER BY created_at", (portfolio_id,))]
    withheld = [Decimal(str(a["baseline"]["cash_withheld_vs_baseline"])) for a in allocs if a.get("baseline")]
    checks = all_rows(app.conn, "SELECT provider, COUNT(*) AS n, SUM(success) AS ok FROM source_check GROUP BY provider")
    fb = one(app.conn, "SELECT COUNT(*) AS n, SUM(useful) AS useful FROM alert_feedback")
    costs = all_rows(app.conn, "SELECT category, COALESCE(SUM(CAST(amount_usd AS REAL)),0) AS s FROM cost_record GROUP BY category")
    enough = len(outcomes) >= MIN_PAUSES_FOR_READOUT
    return {
        "recommendations_compared": len(recs), "eligibility_diverged": len(diverged),
        "pause_codes": _count([c for r in diverged for c in [x["code"] for x in from_json(r["payload_json"])["current_conditions"]["pauses"]]]),
        "allocations_compared": len(withheld), "cash_withheld_vs_baseline_total": sum(withheld, Decimal(0)),
        "cash_withheld_vs_baseline_last": withheld[-1] if withheld else None,
        "paused_name_outcomes": outcomes,
        "source_quality": [{"provider": c["provider"], "checks": c["n"], "success_rate": (c["ok"] or 0) / c["n"]} for c in checks],
        "alert_usefulness": {"rated": fb["n"], "useful": fb["useful"] or 0},
        "costs": {c["category"]: c["s"] for c in costs},
        "verdict": ("readout allowed (still association only)" if enough else
                    f"insufficient evidence: {len(outcomes)} matured pauses (< {MIN_PAUSES_FOR_READOUT}); no claim either way"),
        "caveat": "Paused-name returns are association, not proof that pausing helped or hurt. Drawdown/benchmark-relative "
                  "comparison of the two variants needs the paper portfolios fed by both allocation variants.",
    }


def _count(xs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out
