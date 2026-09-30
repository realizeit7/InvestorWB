"""Fundamental-only baseline vs market-context-augmented system (prospective).

Both variants share the same long-term engine and limits; the baseline ignores current-conditions pauses.
Stored per recommendation (``baseline_eligibility`` vs ``purchase_eligibility``) and per allocation
proposal (``baseline`` block), so the comparison is computed from records made at decision time.

Reported (all DESCRIPTIVE — no statistical test is run and nothing here establishes an edge):
- pause EPISODES: consecutive PAUSED-while-baseline-ELIGIBLE ADD recommendations for one security collapse into
  one episode (repeated daily rows are not independent observations); episodes that start on the same day with
  the same pause codes are grouped as one likely shared cause;
- for matured episodes only, returns over a FIXED horizon of ``HORIZON_SESSIONS`` sessions from the episode start
  (never "until today") for the stock, SPY and the sector benchmark;
- cash withheld relative to the baseline PER PROPOSAL (each proposal is a separate what-if on the same cash, so
  the figures are never summed), plus the latest;
- source quality, alert usefulness (owner feedback) and costs.
A handful of episodes — or 20 — is not a statistical validation; the verdict stays descriptive.
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

HORIZON_SESSIONS = 63          # defaults; active values: policy.market.evaluation_horizon_sessions / _min_episodes
MIN_EPISODES_FOR_SUMMARY = 20


def _horizon_end(start: date, sessions: int = HORIZON_SESSIONS) -> date:
    d = cal.session_on_or_after(start)
    for _ in range(sessions):
        d = cal.next_session(d)
    return d


def _fwd(app: App, sid: str | None, start: date, end: date) -> float | None:
    if sid is None:
        return None
    s = [x for x in tr_series(app, sid, end, (end - start).days + 10) if start <= x[0] <= end]
    if len(s) < 2 or s[-1][0] < end - timedelta(days=5):
        return None                     # horizon not covered by data: unknown, not zero
    return s[-1][1] / s[0][1] - 1


def pause_episodes(app: App, portfolio_id: str) -> list[dict]:
    """Collapse consecutive recommendation rows into independent pause episodes per security."""
    rows = all_rows(app.conn, "SELECT r.*, s.symbol FROM recommendation r JOIN security s ON s.id=r.security_id WHERE "
                              "r.portfolio_id=? AND r.purchase_eligibility IS NOT NULL ORDER BY r.security_id, r.as_of, "
                              "r.created_at", (portfolio_id,))
    episodes: list[dict] = []
    open_: dict[str, dict] = {}
    for r in rows:
        paused = r["action"] == "ADD" and r["purchase_eligibility"] == "PAUSED" and r["baseline_eligibility"] == "ELIGIBLE"
        ep = open_.get(r["security_id"])
        if paused:
            codes = sorted({x["code"] for x in from_json(r["payload_json"])["current_conditions"]["pauses"]})
            if ep is None:
                ep = {"security_id": r["security_id"], "symbol": r["symbol"], "start": r["as_of"], "end": None,
                      "rows": 0, "codes": codes, "exposure_version_id": r["exposure_version_id"]}
                open_[r["security_id"]] = ep
                episodes.append(ep)
            ep["rows"] += 1
            ep["codes"] = sorted(set(ep["codes"]) | set(codes))
        elif ep is not None:
            ep["end"] = r["as_of"]
            del open_[r["security_id"]]
    return episodes


def compare(app: App, portfolio_id: str) -> dict:
    recs = all_rows(app.conn, "SELECT purchase_eligibility, baseline_eligibility FROM recommendation WHERE "
                              "portfolio_id=? AND purchase_eligibility IS NOT NULL", (portfolio_id,))
    diverged = sum(1 for r in recs if r["purchase_eligibility"] != r["baseline_eligibility"])
    session = cal.latest_completed_session(app.now())
    spy = find_security(app.conn, "SPY")
    episodes = pause_episodes(app, portfolio_id)
    horizon, min_eps = app.policy.market.evaluation_horizon_sessions, app.policy.market.evaluation_min_episodes
    matured = []
    for ep in episodes:
        start = parse_utc(ep["start"]).date()
        end = _horizon_end(start, horizon)
        ep["horizon_end"] = end
        if end > session:
            continue
        etf = None
        if ep["exposure_version_id"]:
            prof = from_json(one(app.conn, "SELECT content_json FROM exposure_profile_version WHERE id=?",
                                 (ep["exposure_version_id"],))["content_json"])
            etf = find_security(app.conn, prof.get("sector_benchmark") or "")
        matured.append({"symbol": ep["symbol"], "paused_on": start, "horizon_end": end, "codes": ep["codes"],
                        "return": _fwd(app, ep["security_id"], start, end), "spy": _fwd(app, spy, start, end),
                        "sector": _fwd(app, etf, start, end)})
    causes = {(str(parse_utc(ep["start"]).date()), tuple(ep["codes"])) for ep in episodes}
    allocs = all_rows(app.conn, "SELECT id, as_of, payload_json FROM allocation_proposal WHERE portfolio_id=? ORDER BY as_of, "
                                "created_at", (portfolio_id,))
    withheld = [{"proposal_id": a["id"], "as_of": a["as_of"],
                 "cash_withheld_vs_baseline": Decimal(str(from_json(a["payload_json"])["baseline"]["cash_withheld_vs_baseline"]))}
                for a in allocs if from_json(a["payload_json"]).get("baseline")]
    checks = all_rows(app.conn, "SELECT provider, COUNT(*) AS n, SUM(success) AS ok FROM source_check GROUP BY provider")
    fb = one(app.conn, "SELECT COUNT(*) AS n, SUM(useful) AS useful FROM alert_feedback")
    costs = all_rows(app.conn, "SELECT category, COALESCE(SUM(CAST(amount_usd AS REAL)),0) AS s FROM cost_record GROUP BY category")
    return {
        "recommendations_compared": len(recs), "rows_with_diverged_eligibility": diverged,
        "pause_episodes": len(episodes), "ongoing_episodes": sum(1 for e in episodes if e["end"] is None),
        "likely_shared_causes": len(causes),
        "pause_codes_by_episode": _count([c for e in episodes for c in e["codes"]]),
        "horizon_sessions": horizon, "matured_episodes": matured,
        "cash_withheld_vs_baseline_per_proposal": withheld,
        "cash_withheld_vs_baseline_latest": withheld[-1]["cash_withheld_vs_baseline"] if withheld else None,
        "source_quality": [{"provider": c["provider"], "checks": c["n"], "success_rate": (c["ok"] or 0) / c["n"]} for c in checks],
        "alert_usefulness": {"rated": fb["n"], "useful": fb["useful"] or 0},
        "costs": {c["category"]: c["s"] for c in costs},
        "verdict": (f"insufficient evidence: {len(matured)} matured pause episodes (< {min_eps}); "
                    "descriptive only, no claim either way" if len(matured) < min_eps else
                    f"descriptive only: {len(matured)} matured episodes from {len(causes)} likely causes; no statistical "
                    "test is run and this does not establish that pausing helps or hurts"),
        "caveat": "Association only. Episodes sharing a cause are not independent; withheld cash per proposal is a what-if "
                  "on the same money and is never summed. Variant performance needs the paper portfolios fed by both "
                  "allocation variants under a FROZEN policy.",
    }


def _count(xs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out
