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
from .screening import percentile_ranks as _pct_ranks

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


# ------------------------------------------------------------------ peer groups (versioned mapping "sic-v1")
SIC_DIVISIONS = (("A", 100, 999), ("B", 1000, 1499), ("C", 1500, 1799), ("D", 2000, 3999), ("E", 4000, 4999),
                 ("F", 5000, 5199), ("G", 5200, 5999), ("H", 6000, 6799), ("I", 7000, 8999), ("J", 9100, 9999))
PEER_LEVELS = ("SIC4", "SIC3", "SIC2", "DIV")
# Nasdaq sector labels (display only) and the SIC-derived sectors they plausibly cover; anything else is flagged
NASDAQ_SIC_COMPATIBLE = {
    "Technology": {"Technology", "Communication Services", "Industrials"},
    "Telecommunications": {"Communication Services", "Technology"},
    "Health Care": {"Health Care", "Materials", "Industrials", "Consumer Staples"},
    "Finance": {"Financials", "Real Estate", "Industrials", "Technology"},
    "Real Estate": {"Real Estate", "Financials", "Construction", "Industrials"},
    "Consumer Discretionary": {"Consumer Discretionary", "Consumer Staples", "Industrials", "Communication Services",
                               "Technology"},
    "Consumer Staples": {"Consumer Staples", "Consumer Discretionary", "Materials", "Agriculture"},
    "Industrials": {"Industrials", "Materials", "Construction", "Technology", "Consumer Discretionary"},
    "Basic Materials": {"Materials", "Energy", "Industrials", "Construction"},
    "Energy": {"Energy", "Materials", "Utilities", "Industrials"},
    "Utilities": {"Utilities", "Energy"},
}


def peer_levels(sic: str | int | None) -> list[str]:
    """Labels from finest to coarsest: 4-digit industry, 3-digit group, 2-digit major group, SIC division."""
    if sic in (None, ""):
        return []
    s = int(sic)
    div = next((d for d, lo, hi in SIC_DIVISIONS if lo <= s <= hi), "OTHER")
    return [f"SIC4:{s:04d}", f"SIC3:{s // 10:03d}", f"SIC2:{s // 100:02d}", f"DIV:{div}"]


def assign_peers(sics: dict[str, str | None], min_size: int) -> dict[str, str]:
    """Each key gets the finest label shared by at least ``min_size`` keys (all keys count at every level); if none,
    ``ALL`` — the documented last resort, flagged in reports. Unknown SIC also falls back to ``ALL``."""
    counts: dict[str, int] = {}
    levels = {k: peer_levels(v) for k, v in sics.items()}
    for ls in levels.values():
        for lab in ls:
            counts[lab] = counts.get(lab, 0) + 1
    return {k: next((lab for lab in ls if counts[lab] >= min_size), "ALL") for k, ls in levels.items()}


def peer_pools(sics: dict[str, str | None], labels: dict[str, str]) -> dict[str, set[str]]:
    """Members of every label in use: everyone whose hierarchy contains it."""
    levels = {k: set(peer_levels(v)) for k, v in sics.items()}
    return {lab: (set(sics) if lab == "ALL" else {k for k, ls in levels.items() if lab in ls})
            for lab in set(labels.values())}


def pooled_percentiles(values: dict[str, Decimal | None], sics: dict[str, str | None], labels: dict[str, str],
                       direction: int) -> dict[str, Decimal]:
    """Percentile of each key within its own peer pool (keys without a value get none)."""
    out: dict[str, Decimal] = {}
    for lab, members in peer_pools(sics, labels).items():
        pct = _pct_ranks([(k, values[k]) for k in members if values.get(k) is not None], direction)
        for k in members:
            if labels.get(k) == lab and k in pct:
                out[k] = pct[k]
    return out


def classification_conflict(nasdaq_sector: str | None, sic: str | int | None) -> str | None:
    from ..data.securities import sic_to_sector
    sic_sector = sic_to_sector(sic)
    ok = NASDAQ_SIC_COMPATIBLE.get(nasdaq_sector or "")
    if ok is None or sic_sector is None or sic_sector in ok:
        return None
    return f"Nasdaq sector '{nasdaq_sector}' vs SIC {sic} ({sic_sector})"


def default_sic_lookup(client: HttpClient) -> Callable[[int], tuple[str | None, str | None]]:
    """SIC from the SEC submissions index (reference data, cached on the issuer row; not evidence)."""
    def lookup(cik: int) -> tuple[str | None, str | None]:
        doc = client.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
        return (doc.get("sic") or None, doc.get("sicDescription") or None)
    return lookup


def classify_universe(app: App, universe: list[UniverseRow],
                      sic_lookup: Callable[[int], tuple[str | None, str | None]] | None) -> tuple[dict[str, str | None], list[str]]:
    """{symbol: SIC} for the universe. Issuers are created (reference rows only); SIC is fetched once and cached."""
    from ..data.securities import get_or_create_issuer, update_issuer_classification
    out, warns, fetched, failed = {}, [], 0, 0
    for u in universe:
        iid = get_or_create_issuer(app.conn, app.now_iso(), name=u.name, cik=str(u.cik))
        sic = one(app.conn, "SELECT sic FROM issuer WHERE id=?", (iid,))["sic"]
        if sic is None and sic_lookup is not None:
            try:
                sic, desc = sic_lookup(u.cik)
                fetched += 1
                if sic:
                    update_issuer_classification(app.conn, iid, sic=str(sic), sic_description=desc, fiscal_year_end=None)
            except Exception as exc:                     # one lookup failing must not stop the run
                failed += 1
                if failed <= 5:
                    warns.append(f"SIC lookup {u.symbol} failed: {exc}"[:200])
        out[u.symbol] = str(sic) if sic else None
    if failed:
        warns.append(f"SIC unknown for {failed} companies after lookup failures (ranked in the ALL pool)")
    return out, warns


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


def prelim_rank(app: App, rows: list[tuple[UniverseRow, dict]], sics: dict[str, str | None] | None = None
                ) -> list[tuple[UniverseRow, dict, Decimal | None, dict]]:
    """Percentiles within SIC peer pools (mapping ``sic-v1``, at least ``sector_relative_min_size`` members, coarser
    levels as fallback, ``ALL`` last). The peer label is recorded in the metrics as ``peer_group``."""
    pol = app.policy.finder
    sics = {u.symbol: (sics or {}).get(u.symbol) for u, _m in rows}
    labels = assign_peers(sics, pol.sector_relative_min_size)
    pct = {k: pooled_percentiles({u.symbol: m.get(k) for u, m in rows}, sics, labels, d)
           for k, d in PRELIM_DIRECTIONS.items()}
    out = []
    for u, m in rows:
        comps = {k: pct[k][u.symbol] for k in PRELIM_DIRECTIONS if u.symbol in pct[k]}
        score = sum(comps.values(), ZERO) / len(comps) if len(comps) >= pol.min_prelim_metrics else None
        m = {**m, "peer_group": labels[u.symbol], "sic": sics[u.symbol],
             "classification_conflict": classification_conflict(u.sector, sics[u.symbol])}
        out.append((u, m, score, comps))
    return out


def peer_growth_medians(app: App, prelim: list[tuple[UniverseRow, dict, Decimal | None, dict]]) -> dict[str, Decimal]:
    """Median 3-year revenue CAGR of each company's peer pool (``conservative_gap.min_peer_count`` members with data;
    coarser SIC levels as fallback; none if even ALL is too small)."""
    import statistics
    cg = app.policy.finder.conservative_gap
    vals = {u.symbol: m.get("revenue_cagr_3y") for u, m, _s, _c in prelim if m.get("revenue_cagr_3y") is not None}
    sics = {u.symbol: m.get("sic") for u, m, _s, _c in prelim if u.symbol in vals}
    labels = assign_peers(sics, cg.min_peer_count)
    pools = peer_pools(sics, labels)
    med = {lab: Decimal(str(statistics.median(vals[k] for k in mem))) for lab, mem in pools.items()
           if len(mem) >= cg.min_peer_count}
    return {k: med[lab] for k, lab in labels.items() if lab in med}


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


ARMS = ("A_QUALITY_VALUE", "B_RAW_GAP", "C_CONSERVATIVE_GAP")


def trailing_dollar_volume(app: App, security_id: str, session, n: int) -> Decimal | None:
    """Median close x volume over the last ``n`` sessions up to ``session``; None if fewer than ``n`` bars have volume."""
    import statistics
    rows = all_rows(app.conn, "SELECT session_date, close, volume FROM price_bar WHERE security_id=? AND session_date<=? "
                              "AND volume IS NOT NULL ORDER BY session_date DESC, provider LIMIT ?",
                    (security_id, session.isoformat(), n * 3))
    seen, vals = set(), []
    for r in rows:                                        # one bar per session (first provider)
        if r["session_date"] in seen:
            continue
        seen.add(r["session_date"])
        vals.append(Decimal(r["close"]) * Decimal(r["volume"]))
        if len(vals) == n:
            break
    return Decimal(str(statistics.median(vals))) if len(vals) == n else None


def _implied_growth(base, price, pol_val, variable) -> Decimal | None:
    from ..valuation.dcf import reverse_dcf
    rv = reverse_dcf(base, price, variable, cap=pol_val.terminal_growth_cap)
    return rv.implied_value if rv.status == "SOLVED" else None


def sensitivity_cases(base, sens) -> dict:
    """One-at-a-time modest changes to the reverse-DCF inputs (POLICY.md §13.3)."""
    from ..valuation.dcf import A, Series, Source
    src = Source(kind="DERIVED", note="finder sensitivity case")
    grow = (1 + sens.annual_dilution) ** base.years
    return {
        "wacc_up": base.model_copy(update={"wacc": A(value=base.wacc.value + sens.wacc_delta, source=src)}),
        "terminal_growth_down": base.model_copy(update={"terminal_growth": A(
            value=base.terminal_growth.value - sens.terminal_growth_delta, source=src)}),
        "margins_down": base.model_copy(update={"ebit_margin": Series(
            values=[v * (1 - sens.margin_relative_delta) for v in base.ebit_margin.values], source=src)}),
        "dilution": base.model_copy(update={"diluted_shares": A(value=base.diluted_shares.value * grow, source=src)}),
    }


def _weighted(comps: dict, weights: dict[str, Decimal]) -> Decimal | None:
    w = {k: v for k, v in weights.items() if v > 0 and comps.get(k) is not None}
    tot = sum(w.values(), ZERO)
    return sum((w[k] * Decimal(comps[k]) for k in w), ZERO) / tot if tot else None


def arm_weights(pol) -> dict[str, dict[str, Decimal]]:
    """The predeclared comparison arms (POLICY.md §13.6). B is the owner-facing shortlist."""
    w = pol.weights
    return {"A_QUALITY_VALUE": {"quality": w["quality"], "value": w["value"]},
            "B_RAW_GAP": {"quality": w["quality"], "value": w["value"], "expectations_gap": w["expectations_gap"],
                          "dcf_margin_of_safety": w.get("dcf_margin_of_safety", ZERO)},
            "C_CONSERVATIVE_GAP": {"quality": w["quality"], "value": w["value"],
                                   "conservative_gap": w["expectations_gap"],
                                   "dcf_margin_of_safety": w.get("dcf_margin_of_safety", ZERO)}}


def deep_score(app: App, items: list[tuple[str, str]], as_of: datetime,
               peer_medians: dict[str, Decimal] | None = None) -> list[DeepResult]:
    """items: (symbol, security_id) already fetched. Point-in-time FactViews at ``as_of``.

    "Expectations gap" = historical 3-year revenue CAGR minus the growth implied by the current price in a reverse DCF
    — reported as *historical growth vs model-implied growth*; it is not a forecast of excess returns. The
    conservative variant (``conservative_gap.version``) shrinks historical growth toward the peer median and caps it."""
    from ..valuation.builder import MissingInputs, build_scenarios
    from ..valuation.dcf import ValuationError, margin_of_safety, run_dcf
    from .screening import ScreenInput, score
    pol = app.policy.finder
    cg, val = pol.conservative_gap, app.policy.valuation
    peer_medians = peer_medians or {}
    session = cal.latest_completed_session(as_of)
    rows_db, prices = {}, {}
    for sym, sid in items:
        r = one(app.conn, "SELECT s.security_type, i.id AS iid, i.sic FROM security s "
                          "LEFT JOIN issuer i ON i.id=s.issuer_id WHERE s.id=?", (sid,))
        if r is not None and r["iid"] is not None:
            rows_db[sid] = (sym, r)
            prices[sid] = price_on_or_before(app, sid, session)
    sics = {sid: r["sic"] for sid, (_sym, r) in rows_db.items()}
    peers = assign_peers(sics, app.policy.screening.min_peer_group_size)
    inputs = [ScreenInput(sid, sym, r["sic"], peers[sid], FactView(app, r["iid"], as_of),
                          prices[sid][1] if prices[sid] else None, r["security_type"] or "COMMON")
              for sid, (sym, r) in rows_db.items()]
    rows = {r.security_id: r for r in score(app, inputs)}
    results: list[DeepResult] = []
    for si in inputs:
        sr = rows[si.security_id]
        px = prices.get(si.security_id)
        res = DeepResult(si.symbol, si.security_id, px[1] if px else None, px[0].isoformat() if px else None,
                         metrics={k: v for k, v in sr.metrics.items()}, exclusion=sr.exclusion_reason)
        res.metrics["peer_group"] = peers[si.security_id]
        if sr.exclusion_reason is None and px is None:
            res.exclusion = "NO_PRICE"
        adv = trailing_dollar_volume(app, si.security_id, session, pol.trailing_liquidity_sessions)
        res.metrics["trailing_median_dollar_volume"] = adv
        if adv is None:
            res.metrics["liquidity_note"] = (f"trailing {pol.trailing_liquidity_sessions}-session volume unknown; "
                                             "universe used a single-session proxy")
        elif res.exclusion is None and adv < pol.min_daily_dollar_volume_usd:
            res.exclusion = f"LIQUIDITY<{pol.min_daily_dollar_volume_usd:,.0f} ({pol.trailing_liquidity_sessions}-session median)"
        if res.exclusion is None:
            years = len(si.fv.annual("revenue"))
            if years < pol.min_years_history:
                res.exclusion = f"HISTORY<{pol.min_years_history}y"
        if res.exclusion is None:
            res.components["quality"] = sr.quality_score
            res.components["value"] = sr.value_score
            try:
                scen = build_scenarios(si.fv, val)
                base = scen["base"]
                dcf = run_dcf(base, val.terminal_growth_cap)
                res.metrics["dcf_base_value_per_share"] = dcf.value_per_share
                res.metrics["dcf_margin_of_safety"] = margin_of_safety(res.price, dcf.value_per_share)
                res.metrics["dcf_review_flags"] = len(base.review_flags)
                implied = _implied_growth(base, res.price, val, pol.expectations_gap_variable)
                res.metrics["implied_revenue_growth"] = implied
                hist = sr.metrics.get("revenue_cagr_3y")
                res.metrics["expectations_gap"] = hist - implied if implied is not None and hist is not None else None
                if res.metrics["expectations_gap"] is None:
                    res.metrics["expectations_gap_note"] = ("reverse DCF not solved" if implied is None
                                                            else "historical revenue CAGR unknown")
                med = peer_medians.get(si.symbol)
                res.metrics["peer_median_growth"] = med
                if hist is not None and med is not None and implied is not None:
                    g = min(cg.shrink_weight * hist + (1 - cg.shrink_weight) * med, med + cg.max_excess_over_peer_median)
                    res.metrics["conservative_growth"] = g
                    res.metrics["conservative_gap"] = g - implied
                    sens = {}
                    for name, case in sensitivity_cases(base, pol.sensitivity).items():
                        try:
                            ig = _implied_growth(case, res.price, val, pol.expectations_gap_variable)
                        except (ValuationError, ZeroDivisionError, InvalidOperation):
                            ig = None
                        sens[name] = None if ig is None else g - ig
                    res.metrics["conservative_gap_sensitivity"] = sens
                    res.metrics["fragile"] = res.metrics["conservative_gap"] > 0 and any(
                        v is None or v <= 0 for v in sens.values())
                else:
                    res.metrics["conservative_gap"] = None
                    res.metrics["conservative_gap_note"] = ("peer median growth unavailable" if med is None
                                                            else "historical growth or implied growth unknown")
            except (MissingInputs, ValuationError, ZeroDivisionError, InvalidOperation) as exc:
                res.metrics["dcf_note"] = f"valuation not computable: {exc}"[:300]
        results.append(res)
    eligible = [r for r in results if r.exclusion is None]
    for comp in ("expectations_gap", "conservative_gap", "dcf_margin_of_safety"):
        pct = _pct_ranks([(r.symbol, r.metrics[comp]) for r in eligible if r.metrics.get(comp) is not None], 1)
        for r in eligible:
            if r.symbol in pct:
                r.components[comp] = pct[r.symbol]
    arms = arm_weights(pol)
    for r in eligible:
        r.metrics["arm_scores"] = {}
        if r.components.get("quality") is None or r.components.get("value") is None:
            r.metrics["score_note"] = "insufficient components (need quality and value)"
            continue
        for arm, w in arms.items():
            gap_key = {"B_RAW_GAP": "expectations_gap", "C_CONSERVATIVE_GAP": "conservative_gap"}.get(arm)
            if gap_key and r.components.get(gap_key) is None:
                continue                                        # a gap arm needs its gap component
            r.metrics["arm_scores"][arm] = _weighted(r.components, w)
        r.score = r.metrics["arm_scores"].get("B_RAW_GAP")
        if r.score is None:
            r.metrics["score_note"] = "insufficient components (need quality, value and the historical-vs-implied gap)"
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
               sic_lookup: Callable[[int], tuple[str | None, str | None]] | None = None,
               as_of: datetime | None = None) -> str:
    """Run stages 0-3 and record everything, including the frozen comparison cohorts (arms A/B/C). Fetchers can be
    injected (tests); defaults use the live sources."""
    as_of = as_of or app.now()
    pol = app.policy.finder
    warnings: list[str] = []
    sources: dict = {"retrieved_at": iso_utc(app.now())}
    sec_client = None
    if nasdaq_rows is None:
        nasdaq_rows, sources["nasdaq_raw"] = fetch_nasdaq_screener(app)
    if sec_map is None or frames is None or deep_fetch is None or sic_lookup is None:
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
    sics, sw = classify_universe(app, universe, sic_lookup or default_sic_lookup(sec_client))
    warnings += sw
    prelim = prelim_rank(app, [(u, prelim_metrics(u, frames, years)[0]) for u in universe], sics)
    medians = peer_growth_medians(app, prelim)
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
    deep = deep_score(app, deep_items, as_of, medians)
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
    info_time = app.now_iso()                 # every input to arms A/B/C exists once this run is recorded
    ph = protocol_hash(app)
    arms = arm_weights(pol)
    for arm in ARMS:
        picks = sorted([d for d in deep if d.metrics.get("arm_scores", {}).get(arm) is not None],
                       key=lambda d: (-d.metrics["arm_scores"][arm], d.symbol))[:pol.shortlist_size]
        insert(app.conn, "finder_cohort", {
            "id": new_id("fco"), "run_id": run_id, "arm": arm, "cohort_no": 1, "protocol_hash": ph,
            "rule_json": to_json({"weights": arms[arm], "size": pol.shortlist_size,
                                  "conservative_gap": pol.conservative_gap.model_dump(mode="json")
                                  if arm == "C_CONSERVATIVE_GAP" else None}),
            "info_time": info_time,
            "members_json": to_json([{"symbol": d.symbol, "security_id": d.security_id, "rank": i,
                                      "score": d.metrics["arm_scores"][arm]} for i, d in enumerate(picks, 1)]),
            "excluded_json": "[]", "created_at": info_time})
    app.audit("finder.run", "finder_run", run_id, {"universe": len(universe), "shortlist": len(shortlist)})
    return run_id


def protocol_hash(app: App) -> str:
    """Identity of the finder rules (selection, gap variant, LLM rule, evaluation protocol) in force."""
    from ..util import stable_hash
    return stable_hash(app.policy.finder.model_dump(mode="json"))


def latest_run(app: App) -> dict | None:
    r = one(app.conn, "SELECT * FROM finder_run ORDER BY created_at DESC, rowid DESC LIMIT 1")
    return dict(r) if r else None


def shortlist(app: App, run_id: str) -> list[dict]:
    return [dict(r) | {"metrics": json.loads(r["metrics_json"]), "scores": json.loads(r["scores_json"])}
            for r in all_rows(app.conn, "SELECT * FROM finder_candidate WHERE run_id=? AND stage='SHORTLIST' ORDER BY rank",
                              (run_id,))]
