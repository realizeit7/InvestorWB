"""Portfolio-level market exposure, counted ONCE.

Broad market risk (SPY/QQQ) is shared by every holding and by the ETF sleeve. It is summarized here as
weight x beta across the whole active portfolio (cash beta 0) instead of being charged again inside each
company's valuation. Sector exposure is summed from company holdings; ETF sector look-through stays
UNKNOWN until reliable holdings data is integrated (see SOURCES.md).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..app import App
from ..data.securities import find_security
from ..ledger.views import portfolio_view
from .metrics import beta_to


def portfolio_market_exposure(app: App, portfolio_id: str, session: date) -> dict:
    view = portfolio_view(app, portfolio_id, session)
    spy, qqq = find_security(app.conn, "SPY"), find_security(app.conn, "QQQ")
    rows, unknown = [], []
    tot = {"SPY": 0.0, "QQQ": 0.0}
    covered = {"SPY": 0.0, "QQQ": 0.0}
    for h in view.holdings:
        w = float(h.weight) if h.weight is not None else None
        r = {"symbol": h.symbol, "weight": w, "type": h.security_type}
        for name, bid in (("SPY", spy), ("QQQ", qqq)):
            b = beta_to(app, h.security_id, bid, session) if bid else None
            r[f"beta_{name}"] = b
            if w is not None and b is not None:
                tot[name] += w * b
                covered[name] += w
        if r["beta_SPY"] is None:
            unknown.append(h.symbol)
        rows.append(r)
    cash_w = float(view.cash / view.nav) if view.nav else None
    etf_w = sum(float(h.weight) for h in view.holdings if h.security_type in ("ETF", "FUND") and h.weight is not None)
    return {
        "as_of": session, "nav": view.nav, "cash_weight": cash_w, "holdings": rows,
        "portfolio_beta_spy": tot["SPY"] if covered["SPY"] else None, "portfolio_beta_qqq": tot["QQQ"] if covered["QQQ"] else None,
        "weight_with_beta": covered["SPY"], "beta_unknown_for": unknown,
        "sector_weights_companies": {k: float(v) for k, v in view.sector_weights.items()},
        "etf_weight_without_lookthrough": etf_w,
        "note": ("market exposure is counted once here (weight x beta, cash = 0); company valuations carry no separate "
                 "market-move penalty. ETF sector look-through is UNKNOWN (no reliable holdings feed integrated)."),
    }
