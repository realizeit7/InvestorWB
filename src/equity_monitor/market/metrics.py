"""Deterministic price metrics for stocks, ETFs and indices.

Returns use the total-return index (raw closes + explicit splits/dividends) so splits never look like
crashes. Attribution splits a recent move into market / sector / company-specific components with
regression betas estimated on the PRIOR year. It is a statistical decomposition of co-movement and is
never presented as a cause.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from ..app import App
from ..data.prices import total_return_index
from ..db.core import all_rows

W_1M, W_3M, W_12M = 21, 63, 252


def tr_series(app: App, security_id: str, end: date, days: int = 800) -> list[tuple[date, float]]:
    idx = total_return_index(app, security_id, end - timedelta(days=days), end)
    return [(d, float(v)) for d, v in sorted(idx.items()) if v and v > 0]


def _ret(series: list[tuple[date, float]], n: int) -> float | None:
    if len(series) <= n:
        return None
    return series[-1][1] / series[-1 - n][1] - 1


def _log_rets(series: list[tuple[date, float]]) -> dict[date, float]:
    return {series[i][0]: math.log(series[i][1] / series[i - 1][1]) for i in range(1, len(series))}


def _beta(y: dict[date, float], x: dict[date, float], dates: list[date]) -> float | None:
    pts = [(x[d], y[d]) for d in dates if d in x and d in y]
    if len(pts) < 60:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    var = sum((p[0] - mx) ** 2 for p in pts)
    if var == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / var


@dataclass
class PriceMetrics:
    last_date: date | None
    ret_1m: float | None
    ret_3m: float | None
    ret_12m: float | None
    drawdown_from_52w_high: float | None
    realized_vol_3m: float | None
    avg_dollar_volume_3m: float | None
    observations: int

    def to_dict(self) -> dict:
        return asdict(self)


def price_metrics(app: App, security_id: str, end: date) -> PriceMetrics:
    s = tr_series(app, security_id, end, 420)
    if not s:
        return PriceMetrics(None, None, None, None, None, None, None, 0)
    last_year = s[-W_12M - 1:]
    peak = max(v for _, v in last_year)
    lr = list(_log_rets(s).values())[-W_3M:]
    vol = (math.sqrt(sum((r - sum(lr) / len(lr)) ** 2 for r in lr) / (len(lr) - 1)) * math.sqrt(252)) if len(lr) > 20 else None
    vols = all_rows(app.conn, "SELECT close, volume FROM price_bar WHERE security_id=? AND session_date<=? AND volume IS NOT NULL "
                              "ORDER BY session_date DESC LIMIT ?", (security_id, end.isoformat(), W_3M))
    adv = (sum(float(r["close"]) * float(r["volume"]) for r in vols) / len(vols)) if len(vols) >= 20 else None
    return PriceMetrics(s[-1][0], _ret(s, W_1M), _ret(s, W_3M), _ret(s, W_12M), s[-1][1] / peak - 1, vol, adv, len(s))


@dataclass
class Attribution:
    window_sessions: int
    stock_return: float | None
    market_component: float | None
    sector_component: float | None
    company_specific: float | None
    beta_market: float | None
    beta_sector_residual: float | None
    note: str = ("statistical decomposition of co-movement using prior-year regression betas; "
                 "it describes association, not causation")

    def to_dict(self) -> dict:
        return asdict(self)


def attribution(app: App, stock_id: str, market_id: str | None, sector_id: str | None, end: date,
                window: int = W_1M) -> Attribution:
    s = tr_series(app, stock_id, end)
    if market_id is None or len(s) <= window + 60:
        return Attribution(window, _ret(s, window) if s else None, None, None, None, None, None)
    m = tr_series(app, market_id, end)
    sr, mr = _log_rets(s), _log_rets(m)
    dates = sorted(sr)
    est, win = dates[-window - W_12M:-window], dates[-window:]
    bm = _beta(sr, mr, est)
    if bm is None:
        return Attribution(window, _ret(s, window), None, None, None, None, None)
    tot = sum(sr[d] for d in win)
    mkt = bm * sum(mr.get(d, 0.0) for d in win)
    sec_c, bs = None, None
    if sector_id:
        x = _log_rets(tr_series(app, sector_id, end))
        bxm = _beta(x, mr, est)
        if bxm is not None:
            xres = {d: x[d] - bxm * mr[d] for d in x if d in mr}
            sres = {d: sr[d] - bm * mr[d] for d in sr if d in mr}
            bs = _beta(sres, xres, est)
            if bs is not None:
                sec_c = bs * sum(xres.get(d, 0.0) for d in win)
    idio = tot - mkt - (sec_c or 0.0)
    to_simple = lambda v: None if v is None else math.exp(v) - 1   # noqa: E731
    return Attribution(window, to_simple(tot), to_simple(mkt), to_simple(sec_c) if sec_c is not None else None,
                       to_simple(idio), bm, bs)


def beta_to(app: App, security_id: str, bench_id: str, end: date) -> float | None:
    s, b = _log_rets(tr_series(app, security_id, end, 420)), _log_rets(tr_series(app, bench_id, end, 420))
    return _beta(s, b, sorted(s)[-W_12M:])
