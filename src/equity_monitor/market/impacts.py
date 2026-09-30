"""Observations -> clusters -> chains: how market, sector and company information bears on one company.

Every consequential observation follows:
    observation -> company relevance -> economic mechanism -> implication (valuation/risk) -> proposed effect

Rules (deterministic, thresholds in ``policy.market``):
- Relevance runs ONLY through the company's approved exposure profile (or company-level identity).
  A market/sector development with no exposure path is CONTEXT and cannot change eligibility.
- Observations are grouped into clusters (one development / one hypothesis). Effects are decided per
  cluster, so an earnings release, the price drop, a short-interest rise and a short-volume spike
  around it count as ONE development, not four confirmations.
- Possible effects: CONTEXT_ONLY, NO_CHANGE, RESEARCH_TASK, PAUSE_PURCHASES. Market information never
  produces ADD/TRIM/EXIT and never relaxes limits. Strength never triggers buying; weakness never
  triggers selling; short interest and options data never trigger selling.
- Missing indicators for HIGH exposures are UNKNOWN and (by policy) pause purchases.
- Valuation-assumption proposals are deduplicated per assumption (one market risk => at most one
  assumption change) and are only PROPOSALS: they must go through a new valuation version + approval.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.sec import filing_severity
from ..db.core import all_rows, one
from ..util import from_json, iso_utc, parse_utc
from .exposures import FACTOR_RISING, Exposure, ExposureProfile
from .metrics import attribution
from .series import latest_as_of, series_as_of
from .sources import assert_use

DIR = {"POSITIVE": 1, "NEGATIVE": -1}
EFFECT_RANK = {"CONTEXT_ONLY": 0, "NO_CHANGE": 1, "RESEARCH_TASK": 2, "PAUSE_PURCHASES": 3}


@dataclass
class Observation:
    key: str
    level: str                 # MARKET | SECTOR | COMPANY
    data_class: str
    source_id: str
    statement: str
    public_at: str | None
    family: str                # clustering family
    at: date                   # date used for clustering
    detail: dict = field(default_factory=dict)
    kind: str = "FACT"         # FACT | INTERPRETATION


@dataclass
class Chain:
    cluster_key: str
    level: str
    observations: list[str]
    relevance: dict
    mechanism: str
    implication: dict
    effect: str
    reason: str
    reassess_condition: str | None = None
    reassess_on: date | None = None
    sources: list[dict] = field(default_factory=list)


@dataclass
class ImpactAnalysis:
    snapshot_id: str | None
    exposure_version_id: str | None
    observations: list[Observation]
    clusters: dict[str, list[str]]
    chains: list[Chain]
    unknowns: list[str]
    valuation_proposals: list[dict]
    developments: dict[str, list[str]]
    stress_downside: dict | None = None

    @property
    def pauses(self) -> list[Chain]:
        return [c for c in self.chains if c.effect == "PAUSE_PURCHASES"]

    @property
    def research(self) -> list[Chain]:
        return [c for c in self.chains if c.effect == "RESEARCH_TASK"]

    def decision_relevant(self) -> list[dict]:
        """Chains that can change a decision (used for the recommendation input hash)."""
        return [{"cluster": c.cluster_key, "effect": c.effect, "reason": c.reason, "obs": sorted(c.observations)}
                for c in self.chains if c.effect in ("PAUSE_PURCHASES", "RESEARCH_TASK")] + \
               [{"unknown": u} for u in self.unknowns] + [{"proposal": p["assumption"]} for p in self.valuation_proposals]


# ------------------------------------------------------------------ observation collection
def market_observations(snapshot: dict) -> list[Observation]:
    out: list[Observation] = []
    session = date.fromisoformat(str(snapshot["session"])[:10])
    for f in snapshot["flags"]:
        dc = "PRICE_RETURN" if f["family"] == "EQUITY_MARKET" else _flag_class(f)
        out.append(Observation(f"flag:{f['flag']}:{f['factor'] or '-'}", "MARKET", dc, "snapshot", f"{f['flag']}: {f['observed']}",
                               snapshot["as_of"], f["family"], session, {"flag": f}))
    for sym in ("SPY", "QQQ"):
        m = snapshot["instruments"].get(sym, {})
        if m.get("status") in ("OK", "STALE"):
            out.append(Observation(f"px:{sym}", "MARKET", "PRICE_RETURN", "yahoo_chart",
                                   f"{sym} 1m {_p(m.get('ret_1m'))}, 3m {_p(m.get('ret_3m'))}, 12m {_p(m.get('ret_12m'))}, "
                                   f"{_p(m.get('drawdown_from_52w_high'))} from 52w high", snapshot["as_of"], "EQUITY_MARKET", session,
                                   {"metrics": m}))
    return out


def _flag_class(f: dict) -> str:
    return {"RATES": "INTEREST_RATE", "CREDIT": "CREDIT_SPREAD", "FX": "FX", "OIL": "SPOT_PRICE", "COPPER": "FUTURES_PRICE",
            "INFLATION": "INFLATION", "LABOR": "EMPLOYMENT", "ACTIVITY": "GROWTH"}.get(f["family"], "PRICE_RETURN")


def _p(x) -> str:
    return "n/a" if x is None else f"{float(x):+.1%}"


def sector_observations(snapshot: dict, profile: ExposureProfile | None, sector: str | None) -> list[Observation]:
    out = []
    session = date.fromisoformat(str(snapshot["session"])[:10])
    sec = snapshot["sectors"].get(sector or "")
    if sec and sec.get("status") in ("OK", "STALE"):
        out.append(Observation(f"sector:{sec['etf']}", "SECTOR", "PRICE_RETURN", "yahoo_chart",
                               f"{sector} ({sec['etf']}) 3m {_p(sec.get('ret_3m'))}, vs SPY {_p(sec.get('relative_to_spy_3m'))}",
                               snapshot["as_of"], f"SECTOR:{sec['etf']}", session, {"metrics": sec}))
    ind_etf = profile.industry_benchmark if profile else None
    m = snapshot["instruments"].get(ind_etf or "", {})
    if ind_etf and m.get("status") in ("OK", "STALE"):
        out.append(Observation(f"industry:{ind_etf}", "SECTOR", "PRICE_RETURN", "yahoo_chart",
                               f"industry benchmark {ind_etf} 3m {_p(m.get('ret_3m'))}", snapshot["as_of"], f"SECTOR:{sec['etf'] if sec else ind_etf}",
                               session, {"metrics": m}))
    return out


def company_observations(app: App, security_id: str, issuer_id: str | None, symbol: str, as_of: datetime,
                         market_id: str | None, sector_id: str | None) -> list[Observation]:
    out: list[Observation] = []
    since = iso_utc(as_of - timedelta(days=45))
    if issuer_id:
        for d in all_rows(app.conn, "SELECT * FROM source_document WHERE issuer_id=? AND public_at>? AND public_at<=? "
                                    "AND doc_type IN ('8-K','8-K/A','10-K','10-Q','10-K/A','10-Q/A') ORDER BY public_at",
                          (issuer_id, since, iso_utc(as_of))):
            sev, label = filing_severity(d["doc_type"], d["items"])
            pub = parse_utc(d["public_at"])
            out.append(Observation(f"filing:{d['accession_no']}:{d['doc_type']}", "COMPANY", "FILING_EVENT", "sec_edgar",
                                   f"{d['doc_type']} filed: {label}", d["public_at"], "FILING", cal.ny_date(pub),
                                   {"severity": sev, "document_id": d["id"], "url": d["source_url"], "anchor": True}))
    session = cal.latest_completed_session(as_of)
    att = attribution(app, security_id, market_id, sector_id, session)
    if att.stock_return is not None:
        out.append(Observation(f"attr:{security_id}:{session}", "COMPANY", "PRICE_RETURN", "yahoo_chart",
                               f"{symbol} 1m {_p(att.stock_return)}: market {_p(att.market_component)}, sector "
                               f"{_p(att.sector_component)}, company-specific {_p(att.company_specific)} (association, not causation)",
                               iso_utc(as_of), "PRICE", session, {"attribution": att.to_dict()}))
    si = [p for p in series_as_of(app, f"finra_si:{symbol}", as_of, since=(as_of - timedelta(days=120)).date()) if p.value]
    if si:
        last = si[-1]
        extra = one(app.conn, "SELECT extra_json, public_at FROM market_observation WHERE series_key=? AND period_date=? "
                              "AND public_at<=? ORDER BY public_at DESC LIMIT 1", (f"finra_si:{symbol}", last.period.isoformat(), iso_utc(as_of)))
        ex = json.loads(extra["extra_json"]) if extra and extra["extra_json"] else {}
        chg = (last.value / si[-2].value - 1) if len(si) > 1 and si[-2].value else None
        out.append(Observation(f"si:{symbol}:{last.period}", "COMPANY", "SHORT_INTEREST", "finra_short_interest",
                               f"short interest {last.value:,.0f} shares at {last.period} (change {_p(chg)}, days to cover "
                               f"{ex.get('days_to_cover', 'n/a')})", last.public_at, "POSITIONING", last.period,
                               {"change": chg, "days_to_cover": ex.get("days_to_cover")}))
    sv = [p for p in series_as_of(app, f"finra_shvol:{symbol}", as_of, since=(as_of - timedelta(days=100)).date()) if p.value is not None]
    if sv:
        avg = sum(float(p.value) for p in sv[-60:]) / len(sv[-60:])
        out.append(Observation(f"shvol:{symbol}:{sv[-1].period}", "COMPANY", "SHORT_SALE_VOLUME", "finra_regsho_daily",
                               f"short-sale volume {float(sv[-1].value):.0%} of FINRA-reported volume (60d avg {avg:.0%}); "
                               f"this is trading activity, not outstanding short interest", sv[-1].public_at, "POSITIONING",
                               sv[-1].period, {"ratio": float(sv[-1].value), "avg60": avg}))
    for e in all_rows(app.conn, "SELECT * FROM external_observation WHERE security_id=? AND published_at<=? AND published_at>?",
                      (security_id, iso_utc(as_of), since)):
        out.append(Observation(f"ext:{e['id']}", "COMPANY", "NEWS_CLAIM", "external_research",
                               f"{e['source_name']}: {e['text']} [{e['verification']}]", e["published_at"], "NEWS",
                               cal.ny_date(parse_utc(e["published_at"])), {"verification": e["verification"], "url": e["url"]},
                               kind="FACT" if e["verification"] == "VERIFIED" else "INTERPRETATION"))
    return out


# ------------------------------------------------------------------ clustering
def cluster(observations: list[Observation], before: int, after: int) -> dict[str, list[str]]:
    clusters: dict[str, list[str]] = {}
    anchors = [o for o in observations if o.detail.get("anchor")]
    for o in observations:
        if o.level == "MARKET":
            key = f"market:{o.family}"
        elif o.level == "SECTOR":
            key = f"sector:{o.family}"
        elif o.detail.get("anchor"):
            key = f"event:{o.key}"
        else:
            near = [a for a in anchors if a.at - timedelta(days=before) <= o.at <= a.at + timedelta(days=after)]
            if near:
                a = min(near, key=lambda a: abs((o.at - a.at).days))
                key = f"event:{a.key}"
            else:
                key = f"company:{o.family}:{o.at.isocalendar().year}-W{o.at.isocalendar().week:02d}"
        clusters.setdefault(key, []).append(o.key)
    return clusters


# ------------------------------------------------------------------ analysis
def _indicator_status(snapshot: dict | None, key: str) -> str:
    if snapshot is None:
        return "MISSING"
    if key.startswith("px:"):
        return snapshot["instruments"].get(key[3:], {}).get("status", "MISSING")
    return snapshot["indicators"].get(key, {}).get("status", "MISSING")


def analyze(app: App, *, security_id: str, issuer_id: str | None, symbol: str, sector: str | None, as_of: datetime,
            snapshot: dict | None, profile: tuple[str, ExposureProfile, list[dict]] | None, last_review_at: str,
            valuation=None) -> ImpactAnalysis:
    mp = app.policy.market
    prof = profile[1] if profile else None
    from ..data.securities import find_security
    market_id = find_security(app.conn, "SPY")
    sector_etf = prof.sector_benchmark if prof else None
    sector_id = find_security(app.conn, sector_etf) if sector_etf else None
    obs: list[Observation] = []
    if snapshot:
        obs += market_observations(snapshot) + sector_observations(snapshot, prof, sector)
    obs += company_observations(app, security_id, issuer_id, symbol, as_of, market_id, sector_id)
    by_key = {o.key: o for o in obs}
    clusters = cluster(obs, mp.cluster_window_before_days, mp.cluster_window_after_days)
    chains: list[Chain] = []
    unknowns: list[str] = []
    proposals: dict[str, dict] = {}
    reassess = as_of.date() + timedelta(days=mp.pause_reassess_days)

    # missing information for HIGH exposures
    if prof:
        for e in prof.exposures:
            inds = e.linked_indicators()
            if not inds:
                continue
            statuses = {k: _indicator_status(snapshot, k) for k in inds}
            if all(s == "MISSING" for s in statuses.values()):
                msg = f"{e.factor} ({e.magnitude}) current conditions UNKNOWN: " + ", ".join(f"{k} MISSING" for k in inds)
                unknowns.append(msg)
                if e.magnitude == "HIGH" and mp.pause_on_unknown_high_exposure:
                    chains.append(Chain(f"unknown:{e.factor}", "MARKET", [], {"status": "LINKED", "exposures": [e.factor]},
                                        f"company is highly exposed to {FACTOR_RISING[e.factor]}, but the indicators are missing",
                                        {"type": "RISK", "detail": "cannot assess current conditions"}, "PAUSE_PURCHASES",
                                        "UNKNOWN_CONDITION_HIGH_EXPOSURE",
                                        f"indicators {', '.join(inds)} become available and are reviewed", reassess))

    for ckey, members in clusters.items():
        os_ = [by_key[k] for k in members]
        level = os_[0].level
        srcs = [{"observation": o.key, "source_id": o.source_id, "data_class": o.data_class, "public_at": o.public_at}
                for o in os_]
        if level == "MARKET":
            chains.append(_market_chain(ckey, os_, prof, mp, reassess, srcs, valuation, snapshot, proposals, app, as_of))
        elif level == "SECTOR":
            chains.append(_sector_chain(ckey, os_, mp, srcs))
        else:
            chains.append(_company_chain(ckey, os_, mp, reassess, srcs, last_review_at, as_of))

    stress = None
    if valuation is not None and prof and any(e.magnitude == "HIGH" and e.valuation_assumption == "wacc" and e.direction == "NEGATIVE"
                                              for e in prof.exposures):
        from ..valuation.dcf import A, ScenarioInputs, ValuationError, run_dcf
        try:
            bear = ScenarioInputs.model_validate(valuation.inputs["bear"])
            stressed = bear.model_copy(update={"wacc": A(value=bear.wacc.value + mp.stress_wacc_shift, source=bear.wacc.source)})
            stress = {"bear_value": valuation.bear, "stressed_bear_value": run_dcf(stressed, app.policy.valuation.terminal_growth_cap).value_per_share,
                      "shift": f"WACC +{mp.stress_wacc_shift}", "label": "illustrative stress of the bear case; context only, not approved"}
        except (ValuationError, KeyError, ValueError):
            stress = None

    dev = {"market": [o.statement for o in obs if o.level == "MARKET"], "sector": [o.statement for o in obs if o.level == "SECTOR"],
           "company": [o.statement for o in obs if o.level == "COMPANY"]}
    chains.sort(key=lambda c: (-EFFECT_RANK[c.effect], c.cluster_key))
    return ImpactAnalysis(snapshot["id"] if snapshot else None, profile[0] if profile else None, obs, clusters, chains, unknowns,
                          list(proposals.values()), dev, stress)


def _market_chain(ckey, os_, prof, mp, reassess, srcs, valuation, snapshot, proposals, app, as_of) -> Chain:
    flags = [o.detail["flag"] for o in os_ if "flag" in o.detail]
    stmt = "; ".join(o.statement for o in os_)
    if ckey == "market:EQUITY_MARKET":
        if flags and mp.pause_on_market_stress:
            return Chain(ckey, "MARKET", [o.key for o in os_], {"status": "POLICY", "exposures": []},
                         "owner opted in to pausing purchases during broad market stress",
                         {"type": "ALLOCATION_PRIORITY", "detail": stmt}, "PAUSE_PURCHASES", "MARKET_STRESS_POLICY",
                         "broad-market stress flags clear (policy.market.pause_on_market_stress)", reassess, srcs)
        return Chain(ckey, "MARKET", [o.key for o in os_], {"status": "CONTEXT", "exposures": [],
                                                           "why": "broad market moves are shared by all holdings; market risk is "
                                                                  "counted once at portfolio level, not per company"},
                     "none applied: market strength does not justify buying and weakness does not justify selling",
                     {"type": "NONE", "detail": stmt}, "CONTEXT_ONLY", "BROAD_MARKET_CONTEXT", sources=srcs)
    factors = {f["factor"]: f for f in flags if f["factor"]}
    linked = [e for e in (prof.exposures if prof else []) if e.factor in factors]
    if not linked:
        return Chain(ckey, "MARKET", [o.key for o in os_],
                     {"status": "CONTEXT", "exposures": [], "why": "no approved exposure links this development to the company"},
                     "none identified", {"type": "NONE", "detail": stmt}, "CONTEXT_ONLY", "NO_EXPOSURE_PATH", sources=srcs)
    effect, reason, mech, impl, cond = "NO_CHANGE", "", [], {"type": "RISK", "detail": []}, None
    for e in linked:
        f = factors[e.factor]
        assert_use(_flag_class(f), "EXPOSURE_TRIGGER")
        d = DIR.get(e.direction)
        if d is None or e.magnitude == "UNKNOWN":
            mech.append(f"{e.factor}: direction/magnitude unclear ({e.direction}/{e.magnitude})")
            if EFFECT_RANK[effect] < EFFECT_RANK["RESEARCH_TASK"]:
                effect, reason = "RESEARCH_TASK", "MECHANISM_UNCLEAR"
                cond = f"owner clarifies the {e.factor} exposure"
            continue
        adverse = f["sign"] * d < 0
        mech.append(f"{e.factor} ({e.magnitude}, {e.direction} when {FACTOR_RISING[e.factor]}): {e.mechanism}")
        if not adverse:
            impl["detail"].append(f"{e.factor}: supportive for the company; no change (strength is not a reason to buy)")
            continue
        impl["detail"].append(f"{e.factor}: adverse; {'downside scenario may be understated' if e.magnitude == 'HIGH' else 'monitor'}")
        if e.magnitude == "HIGH" and mp.pause_on_adverse_high_exposure:
            effect, reason = "PAUSE_PURCHASES", f"ADVERSE_{e.factor}_HIGH_EXPOSURE"
            cond = f"{f['flag']} clears, or owner re-approves the valuation/exposure with this condition considered"
        if e.valuation_assumption == "wacc" and f["family"] == "RATES" and valuation is not None:
            _rate_proposal(app, e, valuation, as_of, mp, proposals, f)
    if impl["detail"] and effect == "NO_CHANGE":
        impl["type"] = "RISK"
    return Chain(ckey, "MARKET", [o.key for o in os_], {"status": "LINKED", "exposures": [e.factor for e in linked]},
                 "; ".join(mech), impl, effect, reason or "LINKED_NO_CHANGE", cond,
                 reassess if effect == "PAUSE_PURCHASES" else None, srcs)


def _rate_proposal(app, e: Exposure, valuation, as_of, mp, proposals: dict, flag: dict) -> None:
    assert_use("INTEREST_RATE", "VALUATION_INPUT")
    if "wacc" in proposals:
        return          # one market risk -> at most one assumption change (no double counting)
    now = latest_as_of(app, "fred:DGS10", as_of)
    then = latest_as_of(app, "fred:DGS10", parse_utc(valuation.evidence_as_of))
    if not now or not then:
        return
    delta = (now.value - then.value) / 100
    if abs(delta) * 100 < mp.valuation_rate_change_proposal_pp:
        return
    base_wacc = Decimal(str(valuation.inputs["base"]["wacc"]["value"]))
    proposals["wacc"] = {"assumption": "wacc", "current": base_wacc, "proposed": base_wacc + delta,
                         "source": {"series": "fred:DGS10", "at_valuation": {"period": then.period, "value": then.value},
                                    "now": {"period": now.period, "value": now.value}},
                         "exposure": e.factor, "status": "PROPOSED — requires a new valuation version and owner approval",
                         "pass_through": "1:1 change in risk-free rate (analyst assumption)"}


def _sector_chain(ckey, os_, mp, srcs) -> Chain:
    rel = None
    for o in os_:
        r = o.detail.get("metrics", {}).get("relative_to_spy_3m")
        if r is not None:
            rel = r
    stmt = "; ".join(o.statement for o in os_)
    if rel is not None and abs(rel) >= float(mp.sector_relative_research):
        return Chain(ckey, "SECTOR", [o.key for o in os_], {"status": "SECTOR_MEMBER", "why": "company belongs to this sector/industry"},
                     "unclear: a large relative sector move may reflect demand, pricing, regulation or sentiment",
                     {"type": "RESEARCH", "detail": stmt}, "RESEARCH_TASK", "SECTOR_DIVERGENCE",
                     "owner checks whether industry fundamentals changed", sources=srcs)
    return Chain(ckey, "SECTOR", [o.key for o in os_], {"status": "SECTOR_MEMBER", "why": "company belongs to this sector/industry"},
                 "none identified", {"type": "NONE", "detail": stmt}, "CONTEXT_ONLY", "SECTOR_CONTEXT", sources=srcs)


def _company_chain(ckey, os_, mp, reassess_default, srcs, last_review_at, as_of) -> Chain:
    keys = [o.key for o in os_]
    anchor = next((o for o in os_ if o.detail.get("anchor")), None)
    stmt = "; ".join(o.statement for o in os_)
    research, rreason = False, ""
    for o in os_:
        if o.data_class == "SHORT_INTEREST":
            assert_use("SHORT_INTEREST", "RESEARCH_TRIGGER")
            dtc = o.detail.get("days_to_cover")
            chg = o.detail.get("change")
            if (dtc is not None and Decimal(str(dtc)) >= mp.short_interest_days_to_cover_research) or \
                    (chg is not None and chg >= mp.short_interest_change_research):
                research, rreason = True, "SHORT_INTEREST_ELEVATED"
        elif o.data_class == "SHORT_SALE_VOLUME":
            assert_use("SHORT_SALE_VOLUME", "CONTEXT")       # never a trigger
        elif o.data_class == "PRICE_RETURN":
            cs = o.detail.get("attribution", {}).get("company_specific")
            if cs is not None and abs(cs) >= float(mp.company_specific_move_research):
                research, rreason = True, "LARGE_COMPANY_SPECIFIC_MOVE"
        elif o.data_class == "NEWS_CLAIM" and o.detail.get("verification") == "VERIFIED":
            research, rreason = True, "VERIFIED_EXTERNAL_CLAIM"
    if anchor is not None:
        sev = anchor.detail["severity"]
        unreviewed = (anchor.public_at or "") > (last_review_at or "")
        age = (as_of.date() - anchor.at).days
        if sev in ("MATERIAL", "CRITICAL") and unreviewed and age <= mp.unreviewed_event_pause_days:
            return Chain(ckey, "COMPANY", keys, {"status": "COMPANY", "why": "company-specific disclosure"},
                         "new disclosure may change thesis or valuation assumptions; related market reactions in the same "
                         "window are treated as part of this one development",
                         {"type": "RESEARCH", "detail": stmt}, "PAUSE_PURCHASES", "UNREVIEWED_MATERIAL_EVENT",
                         f"owner reviews {anchor.statement} and records a decision or re-approves thesis/valuation",
                         anchor.at + timedelta(days=mp.unreviewed_event_pause_days), srcs)
        return Chain(ckey, "COMPANY", keys, {"status": "COMPANY", "why": "company-specific disclosure"},
                     "reviewed or informational disclosure", {"type": "NONE" if not research else "RESEARCH", "detail": stmt},
                     "RESEARCH_TASK" if research else "CONTEXT_ONLY", rreason or "EVENT_REVIEWED_OR_INFO",
                     "owner investigates" if research else None, sources=srcs)
    if research:
        return Chain(ckey, "COMPANY", keys, {"status": "COMPANY", "why": "company-level market information"},
                     "unclear: may reflect information not yet in filings, hedging, or noise",
                     {"type": "RESEARCH", "detail": stmt}, "RESEARCH_TASK", rreason,
                     "owner looks for a primary-source explanation (filings, IR releases)", sources=srcs)
    return Chain(ckey, "COMPANY", keys, {"status": "COMPANY", "why": "company-level market information"},
                 "none identified (co-movement is not causation)", {"type": "NONE", "detail": stmt}, "CONTEXT_ONLY",
                 "COMPANY_CONTEXT", sources=srcs)
