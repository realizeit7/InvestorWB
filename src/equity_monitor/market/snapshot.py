"""Shared, timestamped market-context snapshot (broad market + sectors + economy).

Built deterministically from point-in-time data (``public_at <= as_of``). Persisted immutably and
referenced by id from every company review of the same run. Each input carries status OK / STALE /
MISSING; missing inputs are listed, never treated as neutral.

Flags are deterministic threshold events (thresholds in ``policy.market``). A flag names the factor it
bears on and the sign of the move under the factor's "rising" convention (see exposures.FACTOR_RISING).
Flags describe conditions; they are not forecasts and do not by themselves change any recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.securities import find_security, register_security
from ..db.core import all_rows, insert, one
from ..util import from_json, iso_utc, new_id, stable_hash, to_json
from .metrics import price_metrics
from .series import MARKET_PROXIES, REFERENCE_ETFS, SECTOR_ETFS, SPECS, series_as_of, value_near
from .sources import DATA_CLASSES

SNAPSHOT_VERSION = "snapshot-1"


def ensure_reference_securities(app: App, extra: list[str] | None = None) -> dict[str, str]:
    out = {}
    for sym in REFERENCE_ETFS + list(SECTOR_ETFS.values()) + (extra or []):
        out[sym] = register_security(app.conn, app.now_iso(), sym, security_type="ETF", source="reference")
    for sym in MARKET_PROXIES:
        out[sym] = register_security(app.conn, app.now_iso(), sym, security_type="ETF" if not sym.startswith(("^",)) and
                                     "=" not in sym else "OTHER", source="reference")
    return out


def _f(x) -> float | None:
    return None if x is None else float(x)


def _indicator(app: App, key: str, as_of: datetime, stale_daily: int, stale_monthly: int) -> dict:
    spec = SPECS[key]
    pts = series_as_of(app, key, as_of, since=(as_of - timedelta(days=500)).date())
    good = [p for p in pts if p.value is not None]
    if not good:
        return {"key": key, "name": spec.name, "status": "MISSING", "data_class": spec.data_class, "source_id": spec.source_id}
    last = good[-1]
    limit = stale_daily if spec.frequency == "DAILY" else stale_monthly
    status = "STALE" if (as_of.date() - last.period).days > limit else "OK"

    def at(days: int):
        p = value_near(good, last.period - timedelta(days=days))
        return p.value if p else None
    v1m, v3m, v12m = at(30), at(91), at(365)
    return {"key": key, "name": spec.name, "status": status, "data_class": spec.data_class, "source_id": spec.source_id,
            "value": last.value, "period": last.period, "public_at": last.public_at, "vintage_basis": last.vintage_basis,
            "chg_1m": None if v1m is None else last.value - v1m, "chg_3m": None if v3m is None else last.value - v3m,
            "pct_3m": None if not v3m else last.value / v3m - 1, "pct_12m": None if not v12m else last.value / v12m - 1,
            "chg_3m_level": None if v3m is None else last.value - v3m, "unit": spec.unit}


def _px(app: App, sym: str, session: date) -> dict:
    sid = find_security(app.conn, sym)
    if sid is None:
        return {"symbol": sym, "status": "MISSING"}
    m = price_metrics(app, sid, session)
    if m.last_date is None:
        return {"symbol": sym, "status": "MISSING", "security_id": sid}
    status = "STALE" if cal.sessions_elapsed(m.last_date, session) > 1 else "OK"
    return {"symbol": sym, "security_id": sid, "status": status, **m.to_dict()}


@dataclass
class Flag:
    flag: str
    factor: str | None        # None = broad-market condition without a single economic factor
    sign: int                 # +1 factor rising, -1 falling (under FACTOR_RISING convention)
    family: str               # clustering family: overlapping signals of one development share a family
    observed: str
    threshold: str
    inputs: list[str]


def _flags(ind: dict, px: dict, mp) -> list[Flag]:
    out: list[Flag] = []

    def ok(k):
        return ind.get(k, {}).get("status") in ("OK", "STALE") and ind[k].get("value") is not None
    if ok("fred:DGS10") and ind["fred:DGS10"].get("chg_1m") is not None:
        c = ind["fred:DGS10"]["chg_1m"]
        if abs(c) >= mp.rates_1m_change_pp:
            s = 1 if c > 0 else -1
            for factor in (("RATES", "REFINANCING") if s > 0 else ("RATES",)):
                out.append(Flag(f"RATES_{'UP' if s > 0 else 'DOWN'}", factor, s, "RATES", f"10y {c:+.2f}pp over ~1m",
                                f"|Δ| >= {mp.rates_1m_change_pp}pp", ["fred:DGS10"]))
    if ok("fred:BAMLH0A0HYM2"):
        lvl, c3 = ind["fred:BAMLH0A0HYM2"]["value"], ind["fred:BAMLH0A0HYM2"].get("chg_3m")
        if lvl >= mp.hy_oas_level_pct or (c3 is not None and c3 >= mp.hy_oas_3m_change_pp):
            obs = f"HY OAS {lvl}%" + (f" ({c3:+.2f}pp over 3m)" if c3 is not None else "")
            for factor in ("CREDIT_CONDITIONS", "REFINANCING"):
                out.append(Flag("CREDIT_TIGHTENING", factor, 1, "CREDIT", obs,
                                f">= {mp.hy_oas_level_pct}% or +{mp.hy_oas_3m_change_pp}pp/3m", ["fred:BAMLH0A0HYM2"]))
    if ok("fred:DTWEXBGS") and ind["fred:DTWEXBGS"].get("pct_3m") is not None and abs(ind["fred:DTWEXBGS"]["pct_3m"]) >= mp.usd_3m_change:
        p = ind["fred:DTWEXBGS"]["pct_3m"]
        out.append(Flag(f"USD_{'UP' if p > 0 else 'DOWN'}", "FX_USD", 1 if p > 0 else -1, "FX", f"broad USD {p:+.1%} over 3m",
                        f"|Δ| >= {mp.usd_3m_change:.0%}", ["fred:DTWEXBGS"]))
    if ok("fred:DCOILWTICO") and ind["fred:DCOILWTICO"].get("pct_3m") is not None and abs(ind["fred:DCOILWTICO"]["pct_3m"]) >= mp.oil_3m_change:
        p = ind["fred:DCOILWTICO"]["pct_3m"]
        out.append(Flag(f"OIL_{'UP' if p > 0 else 'DOWN'}", "COMMODITY_OIL", 1 if p > 0 else -1, "OIL", f"WTI spot {p:+.1%} over 3m",
                        f"|Δ| >= {mp.oil_3m_change:.0%}", ["fred:DCOILWTICO"]))
    cu = px.get("HG=F", {})
    if cu.get("ret_3m") is not None and abs(cu["ret_3m"]) >= float(mp.copper_3m_change):
        out.append(Flag(f"COPPER_{'UP' if cu['ret_3m'] > 0 else 'DOWN'}", "COMMODITY_COPPER", 1 if cu["ret_3m"] > 0 else -1, "COPPER",
                        f"copper futures {cu['ret_3m']:+.1%} over 3m (futures price, not a spot forecast)",
                        f"|Δ| >= {mp.copper_3m_change:.0%}", ["px:HG=F"]))
    if ok("fred:CPIAUCSL") and ind["fred:CPIAUCSL"].get("pct_12m") is not None and ind["fred:CPIAUCSL"]["pct_12m"] >= mp.cpi_yoy_high:
        out.append(Flag("INFLATION_HIGH", "INFLATION_INPUT_COSTS", 1, "INFLATION", f"CPI {ind['fred:CPIAUCSL']['pct_12m']:.1%} y/y",
                        f">= {mp.cpi_yoy_high:.0%}", ["fred:CPIAUCSL"]))
    if ok("fred:UNRATE") and ind["fred:UNRATE"].get("chg_3m") is not None and ind["fred:UNRATE"]["chg_3m"] >= mp.unemployment_3m_rise_pp:
        obs = f"unemployment {ind['fred:UNRATE']['chg_3m']:+.1f}pp over 3m"
        for factor in ("EMPLOYMENT", "CONSUMER_SPENDING"):
            out.append(Flag("LABOR_WEAKENING", factor, -1, "LABOR", obs, f">= +{mp.unemployment_3m_rise_pp}pp", ["fred:UNRATE"]))
    if ok("fred:INDPRO") and ind["fred:INDPRO"].get("pct_12m") is not None and ind["fred:INDPRO"]["pct_12m"] <= mp.indpro_yoy_contraction:
        out.append(Flag("INDUSTRIAL_CONTRACTION", "ENTERPRISE_SPENDING", -1, "ACTIVITY",
                        f"industrial production {ind['fred:INDPRO']['pct_12m']:+.1%} y/y", f"<= {mp.indpro_yoy_contraction:.0%}",
                        ["fred:INDPRO"]))
    spy, vix = px.get("SPY", {}), px.get("^VIX", {})
    if spy.get("drawdown_from_52w_high") is not None and spy["drawdown_from_52w_high"] <= -float(mp.market_drawdown):
        out.append(Flag("MARKET_DRAWDOWN", None, -1, "EQUITY_MARKET", f"SPY {spy['drawdown_from_52w_high']:.1%} from 52w high",
                        f"<= -{mp.market_drawdown:.0%}", ["px:SPY"]))
    if vix.get("last_close") is not None and Decimal(str(vix["last_close"])) >= mp.vix_elevated:
        out.append(Flag("VOLATILITY_ELEVATED", None, -1, "EQUITY_MARKET", f"VIX {vix['last_close']:.1f} (implied, not a probability)",
                        f">= {mp.vix_elevated}", ["px:^VIX"]))
    return out


def build_snapshot(app: App, as_of: datetime | None = None, extra_etfs: list[str] | None = None) -> str:
    """Compute and persist (or reuse an identical) snapshot; returns its id."""
    as_of = as_of or app.now()
    mp = app.policy.market
    session = cal.latest_completed_session(as_of)
    ensure_reference_securities(app, extra_etfs)
    ind = {k: _indicator(app, k, as_of, mp.stale_daily_days, mp.stale_monthly_days) for k in SPECS}
    px = {}
    for sym in REFERENCE_ETFS + list(MARKET_PROXIES) + sorted(set(extra_etfs or [])):
        px[sym] = _px(app, sym, session)
        sid = px[sym].get("security_id")
        if sid and px[sym]["status"] != "MISSING":
            last = one(app.conn, "SELECT close FROM price_bar WHERE security_id=? AND session_date<=? ORDER BY session_date DESC LIMIT 1",
                       (sid, session.isoformat()))
            px[sym]["last_close"] = float(last["close"]) if last else None
    sectors = {}
    spy3 = px["SPY"].get("ret_3m")
    for sector, etf in SECTOR_ETFS.items():
        m = _px(app, etf, session)
        m["relative_to_spy_3m"] = (m["ret_3m"] - spy3) if (m.get("ret_3m") is not None and spy3 is not None) else None
        sectors[sector] = {"etf": etf, **m}
    vix, vix3m = px.get("^VIX", {}), px.get("^VIX3M", {})
    term = (vix["last_close"] / vix3m["last_close"]) if vix.get("last_close") and vix3m.get("last_close") else None
    flags = _flags(ind, px, mp)
    missing = [k for k, v in ind.items() if v["status"] == "MISSING"] + [s for s, v in px.items() if v["status"] == "MISSING"] + \
              [f"{s} ({v['etf']})" for s, v in sectors.items() if v["status"] == "MISSING"]
    stale = [k for k, v in ind.items() if v["status"] == "STALE"] + [s for s, v in px.items() if v["status"] == "STALE"]
    backfilled = [k for k, v in ind.items() if v.get("vintage_basis") == "CURRENT_VINTAGE_BACKFILL"]
    content = {
        "version": SNAPSHOT_VERSION, "as_of": iso_utc(as_of), "session": session, "indicators": ind, "instruments": px,
        "sectors": sectors, "implied_vol_term_ratio": term, "flags": [f.__dict__ for f in flags], "missing": missing,
        "stale": stale, "revision_caveat": backfilled,
        "unavailable": ["single-stock options (IV/skew/term structure/OI)", "ETF fund flows", "ETF holdings look-through",
                        "futures positioning (CFTC COT, deferred)", "licensed news feed"],
        "notes": ["Price co-movement is association, not causation.",
                  "Implied volatility is a market price of options, not a probability of any business outcome."],
    }
    h = stable_hash(content)
    row = one(app.conn, "SELECT id FROM market_snapshot WHERE session_date=? AND content_hash=?", (session.isoformat(), h))
    if row:
        return row["id"]
    sid = new_id("mks")
    insert(app.conn, "market_snapshot", {"id": sid, "as_of": iso_utc(as_of), "session_date": session.isoformat(), "content_hash": h,
                                         "content_json": to_json(content), "policy_version_id": app.policy_version_id(),
                                         "created_at": app.now_iso()})
    app.audit("market.snapshot", "market_snapshot", sid, {"flags": [f.flag for f in flags], "missing": len(missing)})
    return sid


def load_snapshot(app: App, snapshot_id: str) -> dict:
    r = one(app.conn, "SELECT * FROM market_snapshot WHERE id=?", (snapshot_id,))
    return {"id": r["id"], "created_at": r["created_at"], **from_json(r["content_json"])}


def snapshot_as_of(app: App, as_of: datetime) -> dict | None:
    r = one(app.conn, "SELECT id FROM market_snapshot WHERE as_of<=? ORDER BY as_of DESC, created_at DESC, rowid DESC LIMIT 1",
            (iso_utc(as_of),))
    return load_snapshot(app, r["id"]) if r else None
