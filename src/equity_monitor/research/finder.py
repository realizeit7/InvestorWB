"""Company finder: discover possibly UNDER-RATED companies from current evidence (POLICY.md §13).

Pipeline (each step deterministic and recorded, append-only):

0. Universe — NYSE/Nasdaq listings (Nasdaq screener snapshot) joined to SEC CIKs (company_tickers_exchange):
   US operating companies, market cap >= ``finder.min_market_cap_usd``, last-session dollar volume >= floor,
   excluded industries (banks, underwriting insurers, REITs, funds/BDCs, broker-dealers, SPACs), partnership units
   (policy), common shares only, one listing per company (most liquid class). Nasdaq's sector labels are coarse and
   sometimes surprising; they only define preliminary percentile pools.
   Market cap is a MARKET_CAP_SNAPSHOT: used ONLY to choose what to screen, never as a valuation input.
1. Preliminary rank — SEC XBRL *frames* (one request per concept and calendar year for all filers): revenue
   growth, operating margin and its trend, FCF margin, FCF yield, FCF consistency. Percentiles within the sector
   (or the whole universe for small sectors); a company needs ``min_prelim_metrics`` metrics — missing data is
   never treated as zero. Frames are approximate and latest-value-only, so this stage only picks what to fetch.
2. Deep dive — the top ``deep_dive_count`` names are fetched in full (filing index + SIC, companyfacts, prices)
   and scored point-in-time with the regular screening engine (peer-relative quality + value) plus:
     * expectations gap = historical 3-year revenue CAGR - revenue growth implied by the current price (reverse DCF
       at the policy's default WACC/margins): the market pricing in much less than the company has delivered;
     * DCF margin of safety at the policy's default (unapproved, ILLUSTRATIVE) assumptions.
   Under-rated score = weighted mean of the available component percentiles (``finder.weights``); the DCF margin of
   safety is shown but weighted 0 by default because it comes from the same model as the expectations gap.
3. Shortlist — top ``shortlist_size``. Stage 2 (``finder_judge``) adds an LLM opinion; nothing here creates a
   recommendation: candidates become research only when the owner promotes them to the watchlist.

"Under-rated" is a hypothesis to research, not a finding; whether shortlists beat the S&P 500 is measured only
prospectively (``evaluate_shortlists``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable

from ..app import App
from ..data import calendar as cal
from ..data.http import HttpClient, ProviderError
from ..data.prices import price_on_or_before
from ..data.rawstore import save_raw
from ..db.core import all_rows, insert, one
from ..market.sources import assert_use
from ..util import dstr, iso_utc, new_id, to_json
from .fundamentals import FactView

ZERO = Decimal(0)
NASDAQ_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/{tax}/{tag}/USD/CY{year}.json"
FRAME_CONCEPTS: dict[str, list[tuple[str, str]]] = {
    "revenue": [("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("us-gaap", "SalesRevenueNet")],
    "operating_income": [("us-gaap", "OperatingIncomeLoss")],
    "cfo": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities")],
    "capex": [("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment")],
}
PRELIM_DIRECTIONS = {"revenue_cagr_3y": 1, "op_margin": 1, "op_margin_trend": 1, "fcf_margin_avg": 1, "fcf_yield": 1,
                     "fcf_positive_years": 1}
_PARTNERSHIP = re.compile(r"\b(common units|class [a-z] units|units representing|limited partner)", re.I)
_NON_COMMON = re.compile(r"\b(warrants?|units?|rights?|preferred|depositary|notes due|debentures|subordinated|"
                         r"trust preferred|when issued|acquisition corp)\b", re.I)


class FinderError(RuntimeError):
    pass


def norm_symbol(s: str) -> str:
    return s.strip().upper().replace("/", "-").replace(".", "-")


def _dec(v) -> Decimal | None:
    if v in (None, "", "NA", "N/A"):
        return None
    try:
        return Decimal(str(v).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


# ------------------------------------------------------------------ fetchers (injectable for offline tests)
def fetch_nasdaq_screener(app: App, client: HttpClient | None = None) -> tuple[list[dict], str]:
    client = client or HttpClient(provider="nasdaq_screener", min_interval_s=1.0, timeout_s=60,
                                  user_agent="Mozilla/5.0 (X11; Linux x86_64) InvestorWB/0.1")
    resp = client.get(NASDAQ_URL, headers={"Accept": "application/json"})
    raw_id = save_raw(app, "nasdaq_screener", NASDAQ_URL, resp.content, "application/json", "json", note="finder universe")
    rows = (resp.json().get("data") or {}).get("rows") or []
    if not rows:
        raise FinderError("Nasdaq screener returned no rows (endpoint changed or blocked)")
    return rows, raw_id


def fetch_frames(app: App, client: HttpClient, years: list[int]) -> tuple[dict, list[str], list[str]]:
    """{(concept, year): {cik: Decimal}} with tag priority applied per company; plus raw ids and warnings."""
    out: dict[tuple[str, int], dict[int, Decimal]] = {}
    raws, warns = [], []
    for concept, tags in FRAME_CONCEPTS.items():
        for y in years:
            merged: dict[int, Decimal] = {}
            for tax, tag in tags:
                url = FRAMES_URL.format(tax=tax, tag=tag, year=y)
                try:
                    resp = client.get(url)
                except ProviderError as exc:
                    if exc.status == 404:
                        warns.append(f"no SEC frame {tag} CY{y} yet")
                        continue
                    raise
                raws.append(save_raw(app, "sec_frames", url, resp.content, "application/json", "json"))
                for d in resp.json().get("data", []):
                    cik = int(d["cik"])
                    if cik not in merged and d.get("val") is not None:        # earlier tags have priority
                        merged[cik] = Decimal(str(d["val"]))
            out[(concept, y)] = merged
    return out, raws, warns


# ------------------------------------------------------------------ stage 0: universe
@dataclass
class UniverseRow:
    symbol: str
    cik: int
    name: str
    exchange: str
    sector: str | None
    industry: str | None
    country: str | None
    market_cap: Decimal
    price: Decimal
    dollar_volume: Decimal


def build_universe(app: App, nasdaq_rows: list[dict], sec_map: list[dict]) -> tuple[list[UniverseRow], dict[str, int]]:
    pol = app.policy.finder
    assert_use("MARKET_CAP_SNAPSHOT", "UNIVERSE_FILTER")
    by_sym = {norm_symbol(str(r["ticker"])): r for r in sec_map if r.get("ticker")}
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    best: dict[int, UniverseRow] = {}
    for r in nasdaq_rows:
        sym = norm_symbol(str(r.get("symbol", "")))
        name = str(r.get("name", ""))
        if _PARTNERSHIP.search(name):
            if pol.exclude_partnerships:
                drop("partnership units (K-1; policy exclude_partnerships)")
                continue
        elif not sym or "^" in sym or _NON_COMMON.search(name):
            drop("not common stock (ADR, SPAC, preferred, warrant, unit, note, when-issued)")
            continue
        sec = by_sym.get(sym)
        if sec is None:
            drop("no SEC CIK")
            continue
        if sec.get("exchange") not in pol.exchanges:
            drop("exchange")
            continue
        if (r.get("country") or "") not in pol.countries:
            drop("country (non-US filer)")
            continue
        if (r.get("sector") or "") in pol.excluded_sectors or (r.get("industry") or "") in pol.excluded_industries:
            drop("excluded industry (bank, insurer, REIT, fund/BDC, broker-dealer, SPAC)")
            continue
        cap, px, vol = _dec(r.get("marketCap")), _dec(r.get("lastsale")), _dec(r.get("volume"))
        if cap is None or px is None or vol is None:
            drop("missing market cap/price/volume")
            continue
        if cap < pol.min_market_cap_usd:
            drop("market cap below floor")
            continue
        dv = px * vol
        if dv < pol.min_daily_dollar_volume_usd:
            drop("dollar volume below floor")
            continue
        row = UniverseRow(sym, int(sec["cik"]), str(sec.get("name") or name), str(sec.get("exchange")), r.get("sector"),
                          r.get("industry"), r.get("country"), cap, px, dv)
        prev = best.get(row.cik)
        if prev is None or row.dollar_volume > prev.dollar_volume:      # one listing per company
            if prev is not None:
                drop("other share class of the same company")
            best[row.cik] = row
        else:
            drop("other share class of the same company")
    return sorted(best.values(), key=lambda u: u.symbol), dropped


# ------------------------------------------------------------------ stage 1: preliminary metrics + rank
def prelim_metrics(u: UniverseRow, frames: dict, years: list[int]) -> tuple[dict[str, Decimal | None], int | None]:
    assert_use("FRAME_FUNDAMENTAL", "UNIVERSE_FILTER")

    def v(concept: str, y: int) -> Decimal | None:
        return frames.get((concept, y), {}).get(u.cik)
    latest = next((y for y in sorted(years, reverse=True) if v("revenue", y) is not None), None)
    m: dict[str, Decimal | None] = {k: None for k in PRELIM_DIRECTIONS}
    if latest is None:
        return m, None
    r0, r3 = v("revenue", latest), v("revenue", latest - 3)
    if r0 and r3 and r0 > 0 and r3 > 0:
        m["revenue_cagr_3y"] = Decimal(str((float(r0) / float(r3)) ** (1 / 3) - 1))
    oi0, oi2, r2 = v("operating_income", latest), v("operating_income", latest - 2), v("revenue", latest - 2)
    if oi0 is not None and r0 and r0 > 0:
        m["op_margin"] = oi0 / r0
        if oi2 is not None and r2 and r2 > 0:
            m["op_margin_trend"] = oi0 / r0 - oi2 / r2
    fcf = {}
    for y in (latest, latest - 1, latest - 2):
        c, x = v("cfo", y), v("capex", y)
        if c is not None and x is not None:
            fcf[y] = c - x
    margins = [fcf[y] / v("revenue", y) for y in fcf if v("revenue", y) and v("revenue", y) > 0]
    if margins:
        m["fcf_margin_avg"] = sum(margins, ZERO) / len(margins)
        m["fcf_positive_years"] = Decimal(sum(1 for f in fcf.values() if f > 0))
    if latest in fcf and u.market_cap > 0:
        m["fcf_yield"] = fcf[latest] / u.market_cap
    return m, latest


def _pct_ranks(values: list[tuple[str, Decimal]], direction: int) -> dict[str, Decimal]:
    if len(values) < 2:
        return {}
    ordered = sorted(values, key=lambda kv: kv[1] * direction)
    n = len(ordered) - 1
    return {k: Decimal(i) / Decimal(n) for i, (k, _v) in enumerate(ordered)}


def prelim_rank(app: App, rows: list[tuple[UniverseRow, dict]]) -> list[tuple[UniverseRow, dict, Decimal | None, dict]]:
    pol = app.policy.finder
    by_sector: dict[str, list] = {}
    for u, m in rows:
        by_sector.setdefault(u.sector or "Unknown", []).append((u, m))
    pools = []
    small = []
    for sector, members in by_sector.items():
        (pools.append(members) if len(members) >= pol.sector_relative_min_size else small.extend(members))
    if small:
        pools.append(small)                                 # small sectors are compared together
    out = []
    for pool in pools:
        pct = {k: _pct_ranks([(u.symbol, m[k]) for u, m in pool if m.get(k) is not None], d)
               for k, d in PRELIM_DIRECTIONS.items()}
        for u, m in pool:
            comps = {k: pct[k][u.symbol] for k in PRELIM_DIRECTIONS if u.symbol in pct[k]}
            score = sum(comps.values(), ZERO) / len(comps) if len(comps) >= pol.min_prelim_metrics else None
            out.append((u, m, score, comps))
    return out


# ------------------------------------------------------------------ stage 2: deep dive + under-rated score
@dataclass
class DeepResult:
    symbol: str
    security_id: str
    price: Decimal | None
    price_date: str | None
    metrics: dict = field(default_factory=dict)
    components: dict = field(default_factory=dict)
    score: Decimal | None = None
    exclusion: str | None = None


def deep_score(app: App, items: list[tuple[str, str]], as_of: datetime) -> list[DeepResult]:
    """items: (symbol, security_id) already fetched. Point-in-time FactViews at ``as_of``."""
    from ..valuation.builder import MissingInputs, build_scenarios
    from ..valuation.dcf import ValuationError, margin_of_safety, reverse_dcf, run_dcf
    from .screening import ScreenInput, score
    pol = app.policy.finder
    session = cal.latest_completed_session(as_of)
    inputs, prices = [], {}
    for sym, sid in items:
        r = one(app.conn, "SELECT s.security_type, i.id AS iid, i.sic, i.industry_group FROM security s "
                          "LEFT JOIN issuer i ON i.id=s.issuer_id WHERE s.id=?", (sid,))
        if r is None or r["iid"] is None:
            continue
        px = price_on_or_before(app, sid, session)
        prices[sid] = px
        inputs.append(ScreenInput(sid, sym, r["sic"], r["industry_group"], FactView(app, r["iid"], as_of),
                                  px[1] if px else None, r["security_type"] or "COMMON"))
    rows = {r.security_id: r for r in score(app, inputs)}
    results: list[DeepResult] = []
    for si in inputs:
        sr = rows[si.security_id]
        px = prices.get(si.security_id)
        res = DeepResult(si.symbol, si.security_id, px[1] if px else None, px[0].isoformat() if px else None,
                         metrics={k: v for k, v in sr.metrics.items()}, exclusion=sr.exclusion_reason)
        if sr.exclusion_reason is None and px is None:
            res.exclusion = "NO_PRICE"
        if res.exclusion is None:
            years = len(si.fv.annual("revenue"))
            if years < pol.min_years_history:
                res.exclusion = f"HISTORY<{pol.min_years_history}y"
        if res.exclusion is None:
            res.components["quality"] = sr.quality_score
            res.components["value"] = sr.value_score
            try:
                scen = build_scenarios(si.fv, app.policy.valuation)
                base = scen["base"]
                dcf = run_dcf(base, app.policy.valuation.terminal_growth_cap)
                res.metrics["dcf_base_value_per_share"] = dcf.value_per_share
                res.metrics["dcf_margin_of_safety"] = margin_of_safety(res.price, dcf.value_per_share)
                res.metrics["dcf_review_flags"] = len(base.review_flags)
                rv = reverse_dcf(base, res.price, pol.expectations_gap_variable,
                                 cap=app.policy.valuation.terminal_growth_cap)
                res.metrics["implied_revenue_growth"] = rv.implied_value if rv.status == "SOLVED" else None
                hist = sr.metrics.get("revenue_cagr_3y")
                if rv.status == "SOLVED" and hist is not None:
                    res.metrics["expectations_gap"] = hist - rv.implied_value
                else:
                    res.metrics["expectations_gap"] = None
                    res.metrics["expectations_gap_note"] = f"reverse DCF {rv.status}" if rv.status != "SOLVED" else \
                        "historical revenue CAGR unknown"
            except (MissingInputs, ValuationError, ZeroDivisionError, InvalidOperation) as exc:
                res.metrics["dcf_note"] = f"valuation not computable: {exc}"[:300]
        results.append(res)
    eligible = [r for r in results if r.exclusion is None]
    for comp, metric in (("expectations_gap", "expectations_gap"), ("dcf_margin_of_safety", "dcf_margin_of_safety")):
        pct = _pct_ranks([(r.symbol, r.metrics[metric]) for r in eligible if r.metrics.get(metric) is not None], 1)
        for r in eligible:
            if r.symbol in pct:
                r.components[comp] = pct[r.symbol]
    for r in eligible:
        weighted = [k for k in ("expectations_gap", "dcf_margin_of_safety") if pol.weights.get(k, ZERO) > 0]
        if r.components.get("quality") is None or r.components.get("value") is None or \
                not any(k in r.components for k in weighted):
            r.metrics["score_note"] = "insufficient components (need quality, value and a weighted valuation-gap component)"
            continue
        w = {k: pol.weights.get(k, ZERO) for k, v in r.components.items() if v is not None and pol.weights.get(k, ZERO) > 0}
        tot = sum(w.values(), ZERO)
        r.score = sum((w[k] * Decimal(r.components[k]) for k in w), ZERO) / tot if tot else None
    return results


# ------------------------------------------------------------------ orchestration
def _register(app: App, u: UniverseRow) -> str:
    from ..data.securities import get_or_create_issuer, register_security
    cik = str(u.cik).zfill(10)
    holder = one(app.conn, "SELECT id FROM issuer WHERE cik=?", (cik,))
    iid = holder["id"] if holder else get_or_create_issuer(app.conn, app.now_iso(), name=u.name, cik=cik)
    return register_security(app.conn, app.now_iso(), u.symbol, security_type="COMMON", issuer_id=iid,
                             exchange=u.exchange, source="finder")


def default_deep_fetch(app: App, sec_client: HttpClient, price_provider) -> Callable[[UniverseRow], str | None]:
    from ..data.prices import refresh_prices
    from ..data.sec import fetch_companyfacts, sync_filings
    from .fundamentals import ingest_companyfacts

    def fetch(u: UniverseRow) -> str | None:
        sid = _register(app, u)
        iid = one(app.conn, "SELECT issuer_id FROM security WHERE id=?", (sid,))["issuer_id"]
        sync_filings(app, sec_client, iid)
        raw, rid = fetch_companyfacts(app, sec_client, iid)
        ingest_companyfacts(app, iid, raw, rid)
        refresh_prices(app, [sid], price_provider, lookback_days=400)
        return sid
    return fetch


def run_finder(app: App, *, nasdaq_rows: list[dict] | None = None, sec_map: list[dict] | None = None,
               frames: dict | None = None, deep_fetch: Callable[[UniverseRow], str | None] | None = None,
               as_of: datetime | None = None) -> str:
    """Run stages 0-3 and record everything. Fetchers can be injected (tests); defaults use the live sources."""
    as_of = as_of or app.now()
    pol = app.policy.finder
    warnings: list[str] = []
    sources: dict = {"retrieved_at": iso_utc(app.now())}
    sec_client = None
    if nasdaq_rows is None:
        nasdaq_rows, sources["nasdaq_raw"] = fetch_nasdaq_screener(app)
    if sec_map is None or frames is None or deep_fetch is None:
        from ..data.sec import load_ticker_map, make_client
        sec_client = make_client(app)
        if sec_map is None:
            sec_map = load_ticker_map(app, sec_client)
    universe, dropped = build_universe(app, nasdaq_rows, sec_map)
    if not universe:
        raise FinderError(f"empty universe after filters: {dropped}")
    years = [as_of.year - k for k in range(1, 6)]           # last five completed calendar years
    if frames is None:
        frames, sources["frames_raw"], fw = fetch_frames(app, sec_client, years)
        warnings += fw
    prelim = prelim_rank(app, [(u, prelim_metrics(u, frames, years)[0]) for u in universe])
    ranked = sorted([p for p in prelim if p[2] is not None], key=lambda p: (-p[2], p[0].symbol))
    run_id = new_id("fnd")
    session = cal.latest_completed_session(as_of)
    deep_items: list[tuple[str, str]] = []
    if deep_fetch is None:
        from ..data.prices import provider_from_settings
        deep_fetch = default_deep_fetch(app, sec_client, provider_from_settings(app))
    for u, m, s, comps in ranked[:pol.deep_dive_count]:
        try:
            sid = deep_fetch(u)
        except Exception as exc:                            # one company failing must not stop the run
            warnings.append(f"deep fetch {u.symbol} failed: {exc}"[:300])
            continue
        if sid:
            deep_items.append((u.symbol, sid))
    deep = deep_score(app, deep_items, as_of)
    shortlist = sorted([d for d in deep if d.score is not None], key=lambda d: (-d.score, d.symbol))[:pol.shortlist_size]
    insert(app.conn, "finder_run", {
        "id": run_id, "as_of": iso_utc(as_of), "session_date": session.isoformat(),
        "policy_version_id": app.policy_version_id(), "params_json": to_json(pol.model_dump(mode="json")),
        "universe_count": len(universe), "prelim_ranked": len(ranked), "deep_count": len(deep),
        "shortlist_count": len(shortlist), "sources_json": to_json({**sources, "universe_dropped": dropped}),
        "warnings_json": to_json(warnings), "label": "CURRENT", "created_at": app.now_iso()})
    by_sym = {u.symbol: u for u in universe}
    for i, (u, m, s, comps) in enumerate(ranked, 1):
        insert(app.conn, "finder_candidate", {
            "id": new_id("fc"), "run_id": run_id, "stage": "PRELIM", "symbol": u.symbol, "cik": str(u.cik).zfill(10),
            "security_id": None, "sector": u.sector, "market_cap_usd": dstr(u.market_cap), "price": dstr(u.price),
            "price_date": None, "metrics_json": to_json(m), "scores_json": to_json(comps), "score": dstr(s), "rank": i,
            "exclusion_reason": None, "created_at": app.now_iso()})
    rank_of = {d.symbol: i for i, d in enumerate(shortlist, 1)}
    for d in deep:
        u = by_sym.get(d.symbol)
        base = {"run_id": run_id, "symbol": d.symbol, "cik": str(u.cik).zfill(10) if u else None,
                "security_id": d.security_id, "sector": u.sector if u else None,
                "market_cap_usd": dstr(u.market_cap) if u else None, "price": dstr(d.price), "price_date": d.price_date,
                "metrics_json": to_json(d.metrics), "scores_json": to_json(d.components), "score": dstr(d.score),
                "exclusion_reason": d.exclusion, "created_at": app.now_iso()}
        insert(app.conn, "finder_candidate", {"id": new_id("fc"), "stage": "DEEP", "rank": None, **base})
        if d.symbol in rank_of:
            insert(app.conn, "finder_candidate", {"id": new_id("fc"), "stage": "SHORTLIST", "rank": rank_of[d.symbol], **base})
    app.audit("finder.run", "finder_run", run_id, {"universe": len(universe), "shortlist": len(shortlist)})
    return run_id


def latest_run(app: App) -> dict | None:
    r = one(app.conn, "SELECT * FROM finder_run ORDER BY created_at DESC, rowid DESC LIMIT 1")
    return dict(r) if r else None


def shortlist(app: App, run_id: str) -> list[dict]:
    return [dict(r) | {"metrics": json.loads(r["metrics_json"]), "scores": json.loads(r["scores_json"])}
            for r in all_rows(app.conn, "SELECT * FROM finder_candidate WHERE run_id=? AND stage='SHORTLIST' ORDER BY rank",
                              (run_id,))]


# ------------------------------------------------------------------ prospective evaluation (descriptive only)
def evaluate_shortlists(app: App) -> dict:
    """Forward total returns of shortlisted names vs SPY over FIXED horizons from each run's session, for matured
    windows only; split by the LLM verdict when one exists. Weekly runs overlap heavily (same names, overlapping
    windows), so observations are not independent; nothing here is a statistical test or evidence of an edge."""
    from ..data.securities import find_security
    from ..evaluation.augmented import _fwd
    session = cal.latest_completed_session(app.now())
    spy = find_security(app.conn, "SPY")
    horizons = app.policy.finder.evaluation_horizons_sessions
    out_rows, names = [], set()
    for run in all_rows(app.conn, "SELECT id, session_date FROM finder_run ORDER BY created_at"):
        start = datetime.fromisoformat(run["session_date"]).date()
        verdicts = {r["symbol"]: r["verdict"] for r in all_rows(
            app.conn, "SELECT symbol, verdict FROM finder_judgment WHERE run_id=? ORDER BY created_at", (run["id"],))}
        for h in horizons:
            end = start
            for _ in range(h):
                end = cal.next_session(end)
            if end > session:
                continue
            spy_r = _fwd(app, spy, start, end)
            picks = []
            for c in shortlist(app, run["id"]):
                r = _fwd(app, c["security_id"], start, end)
                picks.append({"symbol": c["symbol"], "return": r, "verdict": verdicts.get(c["symbol"])})
                names.add(c["symbol"])
            known = [p for p in picks if p["return"] is not None]

            def summary(ps):
                if not ps or spy_r is None:
                    return None
                mean = sum(p["return"] for p in ps) / len(ps)
                return {"n": len(ps), "mean_return": mean, "mean_excess_vs_spy": mean - spy_r,
                        "share_beating_spy": sum(1 for p in ps if p["return"] > spy_r) / len(ps)}
            out_rows.append({"run_id": run["id"], "start": start, "horizon_sessions": h, "end": end, "spy_return": spy_r,
                             "picks": len(picks), "with_price_data": len(known), "all": summary(known),
                             "research_further": summary([p for p in known if p["verdict"] == "RESEARCH_FURTHER"]),
                             "likely_value_trap": summary([p for p in known if p["verdict"] == "LIKELY_VALUE_TRAP"])})
    return {"matured_windows": out_rows, "distinct_companies": len(names),
            "verdict": "descriptive only — overlapping weekly shortlists are not independent observations; no "
                       "statistical test is run and nothing here establishes a stock-selection edge",
            "missing": "returns are UNKNOWN (not zero) where price history does not cover the window"}
