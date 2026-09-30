"""Derived portfolio views. Always recomputed from the ledger; never stored as truth."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.prices import price_on_or_before
from ..data.securities import security_ref
from .replay import AccountState, ExternalFlow, RealizedGain, replay_account, realized_total
from .store import accounts_of, load_events, open_issues, portfolio_kind

ZERO = Decimal(0)


@dataclass
class HoldingView:
    security_id: str
    symbol: str
    security_type: str
    issuer_id: str | None
    issuer_name: str | None
    sector: str
    shares: Decimal
    price: Decimal | None
    price_date: date | None
    price_stale_sessions: int | None
    market_value: Decimal | None
    cost_basis: Decimal | None             # None => unknown (at least one lot lacks basis)
    unknown_basis_shares: Decimal
    unrealized_gain: Decimal | None
    dividends: Decimal
    weight: Decimal | None = None
    frozen_reason: str | None = None
    lots: list[dict] = field(default_factory=list)


@dataclass
class PortfolioView:
    portfolio_id: str
    kind: str
    as_of: date
    holdings: list[HoldingView]
    cash: Decimal
    available_cash: Decimal
    unsettled_cash: Decimal
    nav: Decimal | None                     # None when any holding lacks a price
    nav_known_part: Decimal
    missing_prices: list[str]
    issuer_weights: dict[str, Decimal]
    sector_weights: dict[str, Decimal]
    realized_gain: Decimal | None
    realized_known: Decimal
    realized_unknown_lots: int
    dividends: Decimal
    interest: Decimal
    fees: Decimal
    external_flows: list[ExternalFlow]
    realized: list[RealizedGain]
    open_issues: list[dict]
    account_states: dict[str, AccountState]

    @property
    def label(self) -> str:
        return self.kind

    def holding(self, security_id: str) -> HoldingView | None:
        for h in self.holdings:
            if h.security_id == security_id:
                return h
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("account_states", None)
        return d


def portfolio_view(app: App, portfolio_id: str, as_of: date | None = None) -> PortfolioView:
    as_of = as_of or cal.latest_completed_session(app.now())
    kind = portfolio_kind(app, portfolio_id)
    accts = accounts_of(app, portfolio_id)
    states = {a["id"]: replay_account(a["id"], load_events(app, a["id"]), as_of=as_of, strict=False,
                                      settlement_days=a["settlement_days"]) for a in accts}
    agg: dict[str, dict] = {}
    cash = avail = unsettled = divs = interest = fees = ZERO
    flows: list[ExternalFlow] = []
    realized: list[RealizedGain] = []
    for st in states.values():
        cash += st.cash
        avail += st.available_cash(as_of)
        unsettled += st.unsettled_cash(as_of)
        divs += st.dividends
        interest += st.interest
        fees += st.fees
        flows += st.external_flows
        realized += st.realized
        for sid, pos in st.positions.items():
            a = agg.setdefault(sid, {"shares": ZERO, "lots": [], "frozen": None, "divs": ZERO})
            a["shares"] += pos.shares
            a["lots"] += pos.lots
            a["frozen"] = a["frozen"] or pos.frozen_reason
        for sid, amt in st.dividends_by_security.items():
            agg.setdefault(sid, {"shares": ZERO, "lots": [], "frozen": None, "divs": ZERO})["divs"] += amt

    holdings: list[HoldingView] = []
    missing = []
    for sid, a in agg.items():
        if a["shares"] == 0:
            continue
        ref = security_ref(app.conn, sid)
        px = price_on_or_before(app, sid, as_of)
        price, pdate = (px[1], px[0]) if px else (None, None)
        mv = a["shares"] * price if price is not None else None
        if price is None:
            missing.append(ref.symbol)
        basis = None if any(l.cost_total is None for l in a["lots"]) else sum((l.cost_total for l in a["lots"]), ZERO)
        holdings.append(HoldingView(
            security_id=sid, symbol=ref.symbol, security_type=ref.security_type, issuer_id=ref.issuer_id,
            issuer_name=ref.issuer_name, sector=(ref.sector or ("ETF/Fund" if ref.security_type in ("ETF", "FUND") else "Unknown")),
            shares=a["shares"], price=price, price_date=pdate,
            price_stale_sessions=cal.sessions_elapsed(pdate, as_of) if pdate else None, market_value=mv,
            cost_basis=basis, unknown_basis_shares=sum((l.quantity for l in a["lots"] if l.cost_total is None), ZERO),
            unrealized_gain=(mv - basis) if (mv is not None and basis is not None) else None, dividends=a["divs"],
            frozen_reason=a["frozen"],
            lots=[{"lot_id": l.lot_id, "acquired": l.acquired, "quantity": l.quantity, "cost_total": l.cost_total}
                  for l in a["lots"]],
        ))
    holdings.sort(key=lambda h: (-(h.market_value or ZERO), h.symbol))
    known = cash + sum((h.market_value for h in holdings if h.market_value is not None), ZERO)
    nav = None if missing else known
    issuer_w: dict[str, Decimal] = {}
    sector_w: dict[str, Decimal] = {}
    if nav and nav > 0:
        for h in holdings:
            h.weight = h.market_value / nav  # type: ignore[operator]
            key = h.issuer_id or h.security_id   # share classes aggregate at issuer level
            issuer_w[key] = issuer_w.get(key, ZERO) + h.weight
            if h.security_type not in ("ETF", "FUND"):
                sector_w[h.sector] = sector_w.get(h.sector, ZERO) + h.weight
    rt, rk, ru = realized_total(realized)
    return PortfolioView(
        portfolio_id=portfolio_id, kind=kind, as_of=as_of, holdings=holdings, cash=cash, available_cash=avail,
        unsettled_cash=unsettled, nav=nav, nav_known_part=known, missing_prices=missing, issuer_weights=issuer_w,
        sector_weights=sector_w, realized_gain=rt, realized_known=rk, realized_unknown_lots=ru, dividends=divs,
        interest=interest, fees=fees, external_flows=flows, realized=realized,
        open_issues=open_issues(app, [a["id"] for a in accts]), account_states=states,
    )
