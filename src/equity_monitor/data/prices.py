"""Market data: raw (unadjusted) daily bars and corporate actions, with provider adapters.

Ledger accounting uses raw prices plus explicit corporate-action events. Total-return series for
comparisons are built from raw closes + dividends + splits in ``total_return_index`` so dividends
and splits are never double counted.

Providers
- ``yahoo_chart``: Yahoo Finance chart endpoint. UNOFFICIAL, undocumented and without a license
  grant for redistribution; suitable only for personal research. Its ``close`` is split-adjusted,
  so we un-adjust with the split events it returns to recover raw closes.
- ``csv``: user-supplied files (date,open,high,low,close,volume[,dividend,split_ratio]).
- ``fixture``: in-memory data for tests and demos (labelled FIXTURE).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..app import App
from ..db.core import all_rows, insert, one
from ..util import D, NY, UTC, dstr, new_id
from . import calendar as cal
from .http import HttpClient, ProviderError
from .rawstore import resolve_quality_issue, save_raw, upsert_quality_issue


@dataclass
class Bar:
    session_date: date
    close: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    note: str | None = None


@dataclass
class Action:
    action_type: str            # SPLIT | CASH_DIVIDEND | MERGER | SPINOFF | ...
    ex_date: date
    ratio_num: Decimal | None = None
    ratio_den: Decimal | None = None
    cash_amount: Decimal | None = None
    pay_date: date | None = None


@dataclass
class PriceFetch:
    bars: list[Bar]
    actions: list[Action] = field(default_factory=list)
    raw: bytes | None = None
    url: str | None = None
    content_type: str | None = None


class PriceProvider:
    name = "base"

    def fetch(self, symbol: str, start: date, end: date) -> PriceFetch:  # pragma: no cover
        raise NotImplementedError


class FixturePriceProvider(PriceProvider):
    name = "fixture"

    def __init__(self, data: dict[str, PriceFetch] | None = None, fail: set[str] | None = None):
        self.data = data or {}
        self.fail = fail or set()

    def fetch(self, symbol: str, start: date, end: date) -> PriceFetch:
        if symbol in self.fail:
            raise ProviderError(self.name, f"simulated failure for {symbol}", retryable=True)
        pf = self.data.get(symbol)
        if pf is None:
            raise ProviderError(self.name, f"no fixture data for {symbol}")
        return PriceFetch([b for b in pf.bars if start <= b.session_date <= end],
                          [a for a in pf.actions if start <= a.ex_date <= end])


class CsvPriceProvider(PriceProvider):
    """Reads ``<dir>/<SYMBOL>.csv``. Closes must be RAW (unadjusted)."""
    name = "csv"

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def fetch(self, symbol: str, start: date, end: date) -> PriceFetch:
        path = self.dir / f"{symbol.upper()}.csv"
        if not path.exists():
            raise ProviderError(self.name, f"missing {path}")
        raw = path.read_bytes()
        bars, actions = [], []
        for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
            d = date.fromisoformat(r["date"])
            if not (start <= d <= end):
                continue
            bars.append(Bar(d, D(r["close"]), D(r.get("open")), D(r.get("high")), D(r.get("low")), D(r.get("volume"))))
            if D(r.get("dividend")):
                actions.append(Action("CASH_DIVIDEND", d, cash_amount=D(r["dividend"])))
            if r.get("split_ratio"):
                num, den = r["split_ratio"].split(":")
                actions.append(Action("SPLIT", d, D(num), D(den)))
        return PriceFetch(bars, actions, raw, str(path), "text/csv")


class YahooChartProvider(PriceProvider):
    name = "yahoo_chart"
    URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient(provider=self.name, user_agent="Mozilla/5.0 (personal research tool)",
                                           min_interval_s=1.0)

    def fetch(self, symbol: str, start: date, end: date) -> PriceFetch:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
        p2 = int((datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)).timestamp())
        url = self.URL.format(symbol=symbol.upper().replace(".", "-"))
        resp = self.client.get(url, params={"period1": p1, "period2": p2, "interval": "1d",
                                            "events": "div,splits", "includeAdjustedClose": "true"})
        raw = resp.content
        bars, actions = parse_yahoo_chart(raw)
        return PriceFetch(bars, actions, raw, resp.url, "application/json")


def parse_yahoo_chart(raw: bytes) -> tuple[list[Bar], list[Action]]:
    doc = json.loads(raw)
    err = doc.get("chart", {}).get("error")
    if err:
        raise ProviderError("yahoo_chart", f"provider error: {err}")
    res = doc["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    ev = res.get("events", {}) or {}
    splits = []
    for s in (ev.get("splits") or {}).values():
        d = datetime.fromtimestamp(s["date"], UTC).astimezone(NY).date()
        splits.append(Action("SPLIT", d, D(s["numerator"]), D(s["denominator"])))
    def factor_after(d: date) -> Decimal:
        f = Decimal(1)
        for s in splits:
            if s.ex_date > d:
                f *= s.ratio_num / s.ratio_den
        return f
    bars = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue   # missing bar stays missing
        d = datetime.fromtimestamp(t, UTC).astimezone(NY).date()
        f = factor_after(d)
        def adj(v):
            return None if v is None else (D(v) * f).quantize(Decimal("0.0001"))
        bars.append(Bar(d, adj(c), adj(q["open"][i]), adj(q["high"][i]), adj(q["low"][i]),
                        D(q["volume"][i]) if q["volume"][i] is not None else None,
                        note="unadjusted from split-adjusted close" if f != 1 else None))
    actions = list(splits)
    for dv in (ev.get("dividends") or {}).values():
        d = datetime.fromtimestamp(dv["date"], UTC).astimezone(NY).date()
        actions.append(Action("CASH_DIVIDEND", d, cash_amount=(D(dv["amount"]) * factor_after(d)).quantize(Decimal("0.000001"))))
    return bars, actions


def provider_from_settings(app: App) -> PriceProvider:
    name = app.settings.market_data_provider
    if name == "yahoo_chart":
        return YahooChartProvider()
    if name == "csv":
        return CsvPriceProvider(app.home / "prices_csv")
    return FixturePriceProvider()


# ------------------------------------------------------------------ storage
def store_fetch(app: App, security_id: str, fetch: PriceFetch, provider: str, *, upto: date | None = None) -> int:
    """Insert bars (only completed sessions up to ``upto``) and actions. Returns bars written."""
    raw_id = save_raw(app, provider, fetch.url, fetch.raw, fetch.content_type, "json") if fetch.raw else None
    n = 0
    for b in fetch.bars:
        if upto is not None and b.session_date > upto:
            continue   # incomplete / in-progress session
        n += insert(app.conn, "price_bar", {
            "security_id": security_id, "session_date": b.session_date.isoformat(), "open": dstr(b.open),
            "high": dstr(b.high), "low": dstr(b.low), "close": dstr(b.close), "volume": dstr(b.volume),
            "provider": provider, "raw_object_id": raw_id, "retrieved_at": app.now_iso(), "adjustment_note": b.note,
        }, or_ignore=True)
    for a in fetch.actions:
        insert(app.conn, "corporate_action", {
            "id": new_id("ca"), "security_id": security_id, "action_type": a.action_type,
            "ex_date": a.ex_date.isoformat(), "pay_date": a.pay_date.isoformat() if a.pay_date else None,
            "ratio_num": dstr(a.ratio_num), "ratio_den": dstr(a.ratio_den), "cash_amount": dstr(a.cash_amount),
            "provider": provider, "raw_object_id": raw_id, "retrieved_at": app.now_iso(),
        }, or_ignore=True)
    return n


def refresh_prices(app: App, security_ids: list[str], provider: PriceProvider, *, lookback_days: int = 400,
                   job_run_id: str | None = None) -> dict[str, dict]:
    """Fetch recent bars per security. Failures are recorded, never reported as 'no change'."""
    upto = cal.latest_completed_session(app.now())
    out = {}
    for sid in security_ids:
        sym = one(app.conn, "SELECT symbol FROM security WHERE id=?", (sid,))["symbol"]
        last = one(app.conn, "SELECT MAX(session_date) AS d FROM price_bar WHERE security_id=?", (sid,))["d"]
        start = (date.fromisoformat(last) - timedelta(days=10)) if last else upto - timedelta(days=lookback_days)
        key = f"price_refresh:{sid}"
        try:
            fetch = provider.fetch(sym, start, upto)
            n = store_fetch(app, sid, fetch, provider.name, upto=upto)
            latest = one(app.conn, "SELECT MAX(session_date) AS d FROM price_bar WHERE security_id=?", (sid,))["d"]
            _record_check(app, provider.name, sid, "PRICES", True, latest, None, job_run_id)
            resolve_quality_issue(app, key)
            out[sid] = {"ok": True, "new_bars": n, "latest": latest}
        except ProviderError as exc:
            _record_check(app, provider.name, sid, "PRICES", False, None, str(exc), job_run_id)
            upsert_quality_issue(app, key, scope="SECURITY", ref_id=sid, code="PRICE_REFRESH_FAILED",
                                 severity="CRITICAL", detail=str(exc))
            out[sid] = {"ok": False, "error": str(exc)}
    return out


def _record_check(app: App, provider: str, subject: str, check_type: str, success: bool, latest: str | None,
                  error: str | None, job_run_id: str | None) -> None:
    insert(app.conn, "source_check", {
        "id": new_id("chk"), "provider": provider, "subject": subject, "check_type": check_type,
        "checked_at": app.now_iso(), "success": int(success), "latest_seen": latest, "error": error,
        "job_run_id": job_run_id,
    })


record_check = _record_check


def price_on_or_before(app: App, security_id: str, d: date) -> tuple[date, Decimal] | None:
    r = one(app.conn, "SELECT session_date, close FROM price_bar WHERE security_id=? AND session_date<=? "
                      "ORDER BY session_date DESC, provider LIMIT 1", (security_id, d.isoformat()))
    if r is None:
        return None
    return date.fromisoformat(r["session_date"]), D(r["close"])


def bar_on(app: App, security_id: str, d: date) -> dict | None:
    r = one(app.conn, "SELECT * FROM price_bar WHERE security_id=? AND session_date=? ORDER BY provider LIMIT 1",
            (security_id, d.isoformat()))
    return dict(r) if r else None


def price_series(app: App, security_id: str, start: date, end: date) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    for r in all_rows(app.conn, "SELECT session_date, close FROM price_bar WHERE security_id=? AND session_date"
                                " BETWEEN ? AND ? ORDER BY session_date, provider", (security_id, start.isoformat(),
                                                                                   end.isoformat())):
        out.setdefault(date.fromisoformat(r["session_date"]), D(r["close"]))
    return out


def actions_for(app: App, security_id: str, start: date | None = None, end: date | None = None,
                action_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM corporate_action WHERE security_id=?"
    params: list = [security_id]
    if start:
        sql += " AND ex_date>=?"
        params.append(start.isoformat())
    if end:
        sql += " AND ex_date<=?"
        params.append(end.isoformat())
    if action_type:
        sql += " AND action_type=?"
        params.append(action_type)
    # de-duplicate across providers by (type, ex_date)
    seen, out = set(), []
    for r in all_rows(app.conn, sql + " ORDER BY ex_date, provider", params):
        k = (r["action_type"], r["ex_date"])
        if k not in seen:
            seen.add(k)
            out.append(dict(r))
    return out


def total_return_index(app: App, security_id: str, start: date, end: date) -> dict[date, Decimal]:
    """Units-held index: start with 1 unit; splits multiply units; dividends reinvested at the ex-date close.

    value(d) = units(d) * raw_close(d). Raw closes + explicit actions => no double counting.
    """
    closes = price_series(app, security_id, start, end)
    acts = actions_for(app, security_id, start + timedelta(days=1), end)
    by_date: dict[date, list[dict]] = {}
    for a in acts:
        by_date.setdefault(date.fromisoformat(a["ex_date"]), []).append(a)
    units = Decimal(1)
    out = {}
    for d in sorted(closes):
        for a in by_date.get(d, []):
            if a["action_type"] == "SPLIT":
                units *= D(a["ratio_num"]) / D(a["ratio_den"])
        for a in by_date.get(d, []):
            if a["action_type"] == "CASH_DIVIDEND" and closes[d] > 0:
                units += units * D(a["cash_amount"]) / closes[d]
        out[d] = units * closes[d]
    return out
