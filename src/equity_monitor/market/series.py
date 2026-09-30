"""Point-in-time market/economic series: registry, storage, providers, as-of queries.

Storage rule: a value is stored with the time it became public. A different value later reported for
the same period (a revision) is a NEW row with its own ``public_at``; an earlier as-of query still sees
the earlier value. Missing values stay missing (UNKNOWN).

Publication time
- FINRA short interest / Reg SHO: estimated from documented lags (``ESTIMATED_LAG``).
- FRED: estimated per series from lag rules (daily market data: next session 18:00 ET; CPI ~mid next
  month; employment first Friday). ``public_at = min(estimate, retrieval time)``.
- FRED graph CSV returns the CURRENT vintage only. Rows first stored by a backfill are marked
  ``CURRENT_VINTAGE_BACKFILL``: for dates before the first retrieval they may include later revisions,
  and the snapshot flags this. Values first seen by a routine refresh are ``FIRST_SEEN``.

Price-based series (ETFs, indices like ^VIX, futures like CL=F) reuse ``price_bar`` via the price provider.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.http import HttpClient, ProviderError
from ..data.prices import record_check
from ..data.rawstore import resolve_quality_issue, save_raw, upsert_quality_issue
from ..db.core import all_rows, insert, one
from ..util import D, NY, dstr, iso_utc, new_id, parse_utc


@dataclass(frozen=True)
class SeriesSpec:
    key: str
    name: str
    source_id: str
    data_class: str
    category: str
    unit: str
    frequency: str
    lag: str            # NEXT_SESSION | CPI | EMPLOYMENT | MONTHLY_MID | FINRA_SI | NEXT_BUSINESS_DAY


FRED_SERIES = [
    SeriesSpec("fred:DGS10", "10-year Treasury yield", "fred", "INTEREST_RATE", "rates", "percent", "DAILY", "NEXT_SESSION"),
    SeriesSpec("fred:DGS2", "2-year Treasury yield", "fred", "INTEREST_RATE", "rates", "percent", "DAILY", "NEXT_SESSION"),
    SeriesSpec("fred:T10Y2Y", "10y-2y Treasury spread", "fred", "INTEREST_RATE", "rates", "percent", "DAILY", "NEXT_SESSION"),
    SeriesSpec("fred:DFF", "Effective federal funds rate", "fred", "INTEREST_RATE", "rates", "percent", "DAILY", "NEXT_SESSION"),
    SeriesSpec("fred:BAMLH0A0HYM2", "ICE BofA US High Yield OAS", "fred", "CREDIT_SPREAD", "credit", "percent", "DAILY", "NEXT_SESSION"),
    SeriesSpec("fred:BAMLC0A0CM", "ICE BofA US Corporate (IG) OAS", "fred", "CREDIT_SPREAD", "credit", "percent", "DAILY", "NEXT_SESSION"),
    SeriesSpec("fred:CPIAUCSL", "CPI, all urban consumers (SA index)", "fred", "INFLATION", "inflation", "index", "MONTHLY", "CPI"),
    SeriesSpec("fred:UNRATE", "Unemployment rate", "fred", "EMPLOYMENT", "employment", "percent", "MONTHLY", "EMPLOYMENT"),
    SeriesSpec("fred:PAYEMS", "Nonfarm payrolls (thousands)", "fred", "EMPLOYMENT", "employment", "thousands", "MONTHLY", "EMPLOYMENT"),
    SeriesSpec("fred:INDPRO", "Industrial production index", "fred", "GROWTH", "growth", "index", "MONTHLY", "MONTHLY_MID"),
    SeriesSpec("fred:RSAFS", "Retail sales (advance, $m)", "fred", "GROWTH", "growth", "USD millions", "MONTHLY", "MONTHLY_MID"),
    SeriesSpec("fred:DTWEXBGS", "Nominal broad US dollar index", "fred", "FX", "fx", "index", "DAILY", "NEXT_SESSION"),
    SeriesSpec("fred:DCOILWTICO", "WTI crude oil spot", "fred", "SPOT_PRICE", "commodities", "USD/bbl", "DAILY", "NEXT_SESSION"),
]

# Price-based reference instruments (stored in price_bar through the price provider).
REFERENCE_ETFS = ["SPY", "QQQ"]
SECTOR_ETFS = {
    "Technology": "XLK", "Financials": "XLF", "Energy": "XLE", "Health Care": "XLV", "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP", "Industrials": "XLI", "Materials": "XLB", "Utilities": "XLU", "Real Estate": "XLRE",
    "Communication Services": "XLC",
}
MARKET_PROXIES = {   # symbol -> (data_class, description)
    "^VIX": ("IMPLIED_VOL_INDEX", "CBOE 30-day S&P 500 implied volatility index"),
    "^VIX3M": ("IMPLIED_VOL_INDEX", "CBOE 3-month S&P 500 implied volatility index"),
    "TLT": ("PRICE_RETURN", "20+ year Treasury bond ETF"),
    "HYG": ("PRICE_RETURN", "High-yield corporate bond ETF"),
    "CL=F": ("FUTURES_PRICE", "WTI crude oil front-month futures (continuous)"),
    "HG=F": ("FUTURES_PRICE", "COMEX copper front-month futures (continuous)"),
}

SPECS: dict[str, SeriesSpec] = {s.key: s for s in FRED_SERIES}


def ensure_series(app: App, spec: SeriesSpec, security_id: str | None = None) -> None:
    insert(app.conn, "market_series", {"series_key": spec.key, "name": spec.name, "source_id": spec.source_id,
                                       "data_class": spec.data_class, "category": spec.category, "unit": spec.unit,
                                       "frequency": spec.frequency, "security_id": security_id,
                                       "created_at": app.now_iso()}, or_ignore=True)


def estimated_public_at(lag: str, period: date) -> datetime:
    if lag == "NEXT_SESSION":
        return datetime.combine(cal.next_session(period), time(18, 0), tzinfo=NY)
    if lag == "NEXT_BUSINESS_DAY":
        return datetime.combine(cal.next_session(period), time(8, 0), tzinfo=NY)
    first_next = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
    if lag == "CPI":
        return datetime.combine(first_next + timedelta(days=14), time(8, 30), tzinfo=NY)
    if lag == "EMPLOYMENT":
        d = first_next
        while d.weekday() != 4:
            d += timedelta(days=1)
        return datetime.combine(d, time(8, 30), tzinfo=NY)
    if lag == "MONTHLY_MID":
        return datetime.combine(first_next + timedelta(days=17), time(9, 15), tzinfo=NY)
    if lag == "FINRA_SI":
        d = period
        for _ in range(8):
            d = cal.next_session(d)
        return datetime.combine(d, time(18, 0), tzinfo=NY)
    raise ValueError(lag)


def store_values(app: App, spec: SeriesSpec, values: list[tuple[date, Decimal | None]], *, raw_id: str | None,
                 public_at_override: dict[date, datetime] | None = None, extra: dict[date, dict] | None = None) -> int:
    """Append values; unchanged values are no-ops, changed values become revision rows."""
    ensure_series(app, spec)
    now = app.now()
    first_retrieval = one(app.conn, "SELECT MIN(retrieved_at) AS t FROM market_observation WHERE series_key=?", (spec.key,))["t"]
    n = 0
    for d, v in values:
        latest = one(app.conn, "SELECT value FROM market_observation WHERE series_key=? AND period_date=? "
                               "ORDER BY public_at DESC LIMIT 1", (spec.key, d.isoformat()))
        if latest is not None and latest["value"] == dstr(v):
            continue
        est = (public_at_override or {}).get(d) or estimated_public_at(spec.lag, d)
        if latest is not None:
            pub, basis = now, "RETRIEVAL"      # a revision is public no later than when we saw it
        else:
            pub, basis = min(est, now), ("PROVIDED" if public_at_override and d in public_at_override else "ESTIMATED_LAG")
        vintage = "CURRENT_VINTAGE_BACKFILL" if (first_retrieval is None and est < now - timedelta(days=45)) else "FIRST_SEEN"
        if latest is not None:
            vintage = "FIRST_SEEN"
        n += insert(app.conn, "market_observation", {
            "id": new_id("mob"), "series_key": spec.key, "period_date": d.isoformat(), "value": dstr(v),
            "public_at": iso_utc(pub), "public_at_basis": basis, "vintage_basis": vintage, "retrieved_at": app.now_iso(),
            "raw_object_id": raw_id, "extra_json": json.dumps((extra or {}).get(d)) if extra and d in extra else None,
        }, or_ignore=True)
    return n


@dataclass
class Point:
    period: date
    value: Decimal | None
    public_at: str
    vintage_basis: str


def series_as_of(app: App, key: str, as_of: datetime, since: date | None = None) -> list[Point]:
    """Latest known value per period with public_at <= as_of."""
    rows = all_rows(app.conn, "SELECT period_date, value, public_at, vintage_basis FROM market_observation WHERE series_key=? "
                              "AND public_at<=? " + ("AND period_date>=? " if since else "") + "ORDER BY period_date, public_at",
                    (key, iso_utc(as_of)) + ((since.isoformat(),) if since else ()))
    by: dict[str, Point] = {}
    for r in rows:
        by[r["period_date"]] = Point(date.fromisoformat(r["period_date"]), D(r["value"]), r["public_at"], r["vintage_basis"])
    return [by[k] for k in sorted(by)]


def latest_as_of(app: App, key: str, as_of: datetime) -> Point | None:
    pts = [p for p in series_as_of(app, key, as_of, since=(as_of - timedelta(days=800)).date()) if p.value is not None]
    return pts[-1] if pts else None


def value_near(points: list[Point], target: date) -> Point | None:
    """Latest point on or before ``target`` (non-missing)."""
    cands = [p for p in points if p.period <= target and p.value is not None]
    return cands[-1] if cands else None


# ------------------------------------------------------------------ providers
def parse_fred_csv(text: str) -> list[tuple[date, Decimal | None]]:
    out = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise ProviderError("fred", "unexpected CSV header")
    for row in reader:
        if len(row) < 2 or not row[0]:
            continue
        v = row[1].strip()
        out.append((date.fromisoformat(row[0]), None if v in ("", ".") else Decimal(v)))
    return out


def refresh_fred(app: App, keys: list[str] | None = None, client: HttpClient | None = None, *, since_days: int = 1500,
                 job_run_id: str | None = None, fetch=None) -> dict:
    out = {}
    if client is None and fetch is None:
        contact = app.settings.sec_user_agent
        if not contact:
            # Verified 2026-09-30: FRED silently stalls requests whose User-Agent lacks a contact email.
            msg = "FRED needs a User-Agent with contact details: set sec_user_agent in config/user.yaml"
            for k in (keys or list(SPECS)):
                record_check(app, "fred", k, "SERIES", False, None, msg, job_run_id)
                out[k] = {"ok": False, "error": msg}
            return out
        client = HttpClient(provider="fred", user_agent=f"InvestorWB/0.1 {contact}", min_interval_s=0.5,
                            timeout_s=15, max_retries=1)
    for spec in [SPECS[k] for k in (keys or list(SPECS))]:
        fid = spec.key.split(":", 1)[1]
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fid}"
        key = f"series_refresh:{spec.key}"
        try:
            text = fetch(url) if fetch else client.get(url).text
            raw_id = save_raw(app, "fred", url, text.encode(), "text/csv", "csv")
            vals = [(d, v) for d, v in parse_fred_csv(text) if d >= (app.now().date() - timedelta(days=since_days))]
            n = store_values(app, spec, vals, raw_id=raw_id)
            latest = max((d for d, v in vals if v is not None), default=None)
            record_check(app, "fred", spec.key, "SERIES", True, latest.isoformat() if latest else None, None, job_run_id)
            resolve_quality_issue(app, key)
            out[spec.key] = {"ok": True, "new_rows": n, "latest": latest}
        except (ProviderError, ValueError) as exc:
            record_check(app, "fred", spec.key, "SERIES", False, None, str(exc), job_run_id)
            upsert_quality_issue(app, key, scope="PROVIDER", ref_id=spec.key, code="SERIES_REFRESH_FAILED",
                                 severity="WARNING", detail=str(exc))
            out[spec.key] = {"ok": False, "error": str(exc)}
    return out


def short_interest_spec(symbol: str) -> SeriesSpec:
    return SeriesSpec(f"finra_si:{symbol}", f"{symbol} short interest (shares)", "finra_short_interest", "SHORT_INTEREST",
                      "positioning", "shares", "SEMIMONTHLY", "FINRA_SI")


def short_volume_spec(symbol: str) -> SeriesSpec:
    return SeriesSpec(f"finra_shvol:{symbol}", f"{symbol} short-sale volume share of FINRA-reported volume",
                      "finra_regsho_daily", "SHORT_SALE_VOLUME", "positioning", "ratio", "DAILY", "NEXT_BUSINESS_DAY")


def parse_finra_short_interest(rows: list[dict]) -> list[tuple[date, Decimal | None, dict]]:
    out = []
    for r in rows:
        d = date.fromisoformat(r["settlementDate"])
        out.append((d, D(r.get("currentShortPositionQuantity")),
                    {"days_to_cover": r.get("daysToCoverQuantity"), "avg_daily_volume": r.get("averageDailyVolumeQuantity"),
                     "revision_flag": r.get("revisionFlag"), "change_pct": r.get("changePercent")}))
    return out


def refresh_short_interest(app: App, symbol: str, *, fetch=None, job_run_id: str | None = None) -> dict:
    spec = short_interest_spec(symbol)
    url = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
    body = {"limit": 100, "compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": symbol}],
            "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": (app.now().date() - timedelta(days=400)).isoformat(),
                                  "endDate": app.now().date().isoformat()}]}
    try:
        if fetch:
            rows = fetch(url, body)
        else:
            import requests
            r = requests.post(url, json=body, headers={"Accept": "application/json"}, timeout=30)
            if r.status_code != 200:
                raise ProviderError("finra", f"HTTP {r.status_code}")
            rows = r.json()
        raw_id = save_raw(app, "finra", url + "#" + symbol, json.dumps(rows, sort_keys=True).encode(), "application/json", "json")
        parsed = parse_finra_short_interest(rows)
        n = store_values(app, spec, [(d, v) for d, v, _ in parsed], raw_id=raw_id, extra={d: e for d, _, e in parsed})
        record_check(app, "finra_short_interest", spec.key, "SERIES", True, max((d for d, _, _ in parsed), default=None) and
                     max(d for d, _, _ in parsed).isoformat(), None, job_run_id)
        return {"ok": True, "new_rows": n}
    except (ProviderError, ValueError, KeyError, OSError) as exc:
        record_check(app, "finra_short_interest", spec.key, "SERIES", False, None, str(exc), job_run_id)
        return {"ok": False, "error": str(exc)}


def parse_regsho(text: str, symbols: set[str]) -> dict[str, tuple[Decimal, Decimal]]:
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) >= 5 and parts[1] in symbols:
            short, total = D(parts[2]), D(parts[4])
            if total:
                out[parts[1]] = (short, total)
    return out


def refresh_short_volume(app: App, symbols: list[str], day: date, *, fetch=None, job_run_id: str | None = None) -> dict:
    url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day:%Y%m%d}.txt"
    try:
        text = fetch(url) if fetch else HttpClient(provider="finra", user_agent="equity-monitor/0.1").get(url).text
        raw_id = save_raw(app, "finra", url, text.encode(), "text/plain", "txt")
        parsed = parse_regsho(text, set(symbols))
        for sym, (short, total) in parsed.items():
            store_values(app, short_volume_spec(sym), [(day, (short / total).quantize(Decimal("0.0001")))], raw_id=raw_id)
        record_check(app, "finra_regsho_daily", "regsho", "SERIES", True, day.isoformat(), None, job_run_id)
        return {"ok": True, "symbols": len(parsed)}
    except (ProviderError, ValueError, OSError) as exc:
        record_check(app, "finra_regsho_daily", "regsho", "SERIES", False, None, str(exc), job_run_id)
        return {"ok": False, "error": str(exc)}
