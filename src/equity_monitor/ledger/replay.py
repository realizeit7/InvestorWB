"""Deterministic ledger replay: events -> positions, FIFO lots, cash, income, realized P&L.

Pure functions over plain ``LedgerEvent`` objects so the same code validates imports,
builds portfolio views, and powers tests. Nothing here touches the database.

Key rules
- Long-only: a SELL larger than the shares held raises ``NegativeHoldingError``.
- Unknown cost basis stays ``None``; realized/unrealized gains that depend on it are unknown.
- Reversed events are excluded; the REVERSAL row itself has no economic effect.
- CORPORATE_ACTION events (mergers, spinoffs, ...) are NOT applied; they surface as issues.
- Cash is debited/credited on trade date. Sale proceeds are *unsettled* until settle date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..data import calendar as cal

ZERO = Decimal("0")

CASH_IN_TYPES = {"DEPOSIT", "OPENING_CASH", "DIVIDEND", "INTEREST"}
EXTERNAL_FLOW_TYPES = {"DEPOSIT", "WITHDRAWAL", "OPENING_CASH", "OPENING_POSITION"}


class LedgerError(ValueError):
    pass


class NegativeHoldingError(LedgerError):
    pass


@dataclass(frozen=True)
class LedgerEvent:
    id: str
    seq: int
    account_id: str
    event_type: str
    trade_date: date
    security_id: str | None = None
    settle_date: date | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    fees: Decimal | None = None
    amount: Decimal | None = None
    cost_basis_total: Decimal | None = None
    ratio_num: Decimal | None = None
    ratio_den: Decimal | None = None
    action_subtype: str | None = None
    link_group_id: str | None = None
    reverses_event_id: str | None = None
    replaces_event_id: str | None = None
    import_row: int | None = None


@dataclass
class Lot:
    lot_id: str                 # source event id (+ suffix after partial sales)
    security_id: str
    acquired: date | None       # None when unknown (e.g. imported holding without date)
    quantity: Decimal
    cost_total: Decimal | None  # None = unknown basis

    @property
    def cost_per_share(self) -> Decimal | None:
        if self.cost_total is None or self.quantity == 0:
            return None
        return self.cost_total / self.quantity


@dataclass
class RealizedGain:
    event_id: str
    security_id: str
    sell_date: date
    quantity: Decimal
    proceeds: Decimal
    cost: Decimal | None
    gain: Decimal | None
    holding_period: str          # SHORT | LONG | UNKNOWN (informational only; not tax advice)
    lot_id: str


@dataclass
class Position:
    security_id: str
    lots: list[Lot] = field(default_factory=list)
    frozen_reason: str | None = None   # set when an unsupported corporate action affects it

    @property
    def shares(self) -> Decimal:
        return sum((l.quantity for l in self.lots), ZERO)

    @property
    def cost_basis(self) -> Decimal | None:
        if any(l.cost_total is None for l in self.lots):
            return None
        return sum((l.cost_total for l in self.lots), ZERO)  # type: ignore[misc]

    @property
    def unknown_basis_shares(self) -> Decimal:
        return sum((l.quantity for l in self.lots if l.cost_total is None), ZERO)


@dataclass
class ExternalFlow:
    on: date
    amount: Decimal | None       # + into portfolio, - out; None = in-kind with unknown value
    kind: str
    security_id: str | None = None
    quantity: Decimal | None = None


@dataclass
class AccountState:
    account_id: str
    cash: Decimal = ZERO
    unsettled: list[tuple[date, Decimal]] = field(default_factory=list)
    positions: dict[str, Position] = field(default_factory=dict)
    realized: list[RealizedGain] = field(default_factory=list)
    dividends: Decimal = ZERO
    dividends_by_security: dict[str, Decimal] = field(default_factory=dict)
    interest: Decimal = ZERO
    fees: Decimal = ZERO
    external_flows: list[ExternalFlow] = field(default_factory=list)
    trades: list[tuple[date, str, str, Decimal, Decimal]] = field(default_factory=list)  # date, side, sec, qty, gross
    issues: list[dict] = field(default_factory=list)
    min_cash: tuple[Decimal, date | None] = (ZERO, None)

    def unsettled_cash(self, as_of: date) -> Decimal:
        return sum((amt for d, amt in self.unsettled if d > as_of), ZERO)

    def available_cash(self, as_of: date) -> Decimal:
        return self.cash - self.unsettled_cash(as_of)

    def held(self) -> dict[str, Position]:
        return {k: p for k, p in self.positions.items() if p.shares != 0}


def default_settle_date(trade_date: date, settlement_days: int) -> date:
    d = trade_date
    for _ in range(settlement_days):
        d = cal.next_session(d)
    return d


def _sort_key(e: LedgerEvent):
    # Same-day order: corporate actions/splits first (effective at open), then cash-in, buys, sells, other.
    rank = {"SPLIT": 0, "CORPORATE_ACTION": 0, "OPENING_CASH": 1, "OPENING_POSITION": 1, "DEPOSIT": 2,
            "DIVIDEND": 3, "INTEREST": 3, "BUY": 4, "SELL": 5, "FEE": 6, "WITHDRAWAL": 7, "REVERSAL": 9}
    return (e.trade_date, rank.get(e.event_type, 8), e.seq)


def effective_events(events: list[LedgerEvent]) -> list[LedgerEvent]:
    reversed_ids = {e.reverses_event_id for e in events if e.event_type == "REVERSAL" and e.reverses_event_id}
    return sorted((e for e in events if e.id not in reversed_ids and e.event_type != "REVERSAL"), key=_sort_key)


def replay_account(account_id: str, events: list[LedgerEvent], *, as_of: date | None = None,
                   settlement_days: int = 1, strict: bool = True) -> AccountState:
    """Replay one account's events up to and including ``as_of``.

    With ``strict=False`` offending events are skipped and recorded in ``state.issues``
    (used by importers to reject individual rows); otherwise the first error raises.
    """
    st = AccountState(account_id=account_id)
    for e in effective_events(events):
        if as_of is not None and e.trade_date > as_of:
            break
        try:
            _apply(st, e, settlement_days)
        except LedgerError as exc:
            if strict:
                raise
            st.issues.append({"type": "REJECTED_EVENT", "event_id": e.id, "import_row": e.import_row,
                              "error": str(exc)})
        if st.cash < st.min_cash[0]:
            st.min_cash = (st.cash, e.trade_date)
    if st.min_cash[0] < 0:
        st.issues.append({"type": "NEGATIVE_CASH", "min_cash": str(st.min_cash[0]),
                          "on": st.min_cash[1].isoformat() if st.min_cash[1] else None})
    return st


def _need(value, name: str, e: LedgerEvent):
    if value is None:
        raise LedgerError(f"{e.event_type} requires {name} (event {e.id}, row {e.import_row})")
    return value


def _apply(st: AccountState, e: LedgerEvent, settlement_days: int) -> None:
    t = e.event_type
    fees = e.fees or ZERO
    if fees < 0:
        raise LedgerError("fees must be >= 0")
    if t in ("DEPOSIT", "OPENING_CASH"):
        amt = _need(e.amount, "amount", e)
        if amt <= 0:
            raise LedgerError(f"{t} amount must be positive")
        st.cash += amt
        st.external_flows.append(ExternalFlow(e.trade_date, amt, t))
    elif t == "WITHDRAWAL":
        amt = _need(e.amount, "amount", e)
        if amt <= 0:
            raise LedgerError("WITHDRAWAL amount must be positive")
        st.cash -= amt
        st.external_flows.append(ExternalFlow(e.trade_date, -amt, t))
    elif t == "FEE":
        amt = _need(e.amount, "amount", e)
        st.cash -= amt
        st.fees += amt
    elif t in ("DIVIDEND", "INTEREST"):
        amt = _need(e.amount, "amount", e)
        st.cash += amt
        if t == "DIVIDEND":
            st.dividends += amt
            if e.security_id:
                st.dividends_by_security[e.security_id] = st.dividends_by_security.get(e.security_id, ZERO) + amt
        else:
            st.interest += amt
    elif t == "BUY":
        q = _need(e.quantity, "quantity", e)
        p = _need(e.price, "price", e)
        sid = _need(e.security_id, "security", e)
        if q <= 0 or p < 0:
            raise LedgerError("BUY quantity must be > 0 and price >= 0")
        pos = st.positions.setdefault(sid, Position(sid))
        if pos.frozen_reason:
            raise LedgerError(f"position frozen pending reconciliation: {pos.frozen_reason}")
        gross = q * p
        st.cash -= gross + fees
        st.fees += fees
        pos.lots.append(Lot(e.id, sid, e.trade_date, q, gross + fees))
        st.trades.append((e.trade_date, "BUY", sid, q, gross))
    elif t == "SELL":
        q = _need(e.quantity, "quantity", e)
        p = _need(e.price, "price", e)
        sid = _need(e.security_id, "security", e)
        if q <= 0 or p < 0:
            raise LedgerError("SELL quantity must be > 0 and price >= 0")
        pos = st.positions.get(sid)
        held = pos.shares if pos else ZERO
        if q > held:
            raise NegativeHoldingError(
                f"SELL of {q} exceeds holding {held} for {sid} on {e.trade_date} (long-only; row {e.import_row})")
        if pos.frozen_reason:
            raise LedgerError(f"position frozen pending reconciliation: {pos.frozen_reason}")
        gross = q * p
        proceeds = gross - fees
        st.cash += proceeds
        st.fees += fees
        settle = e.settle_date or default_settle_date(e.trade_date, settlement_days)
        st.unsettled.append((settle, proceeds))
        _consume_lots(st, pos, e, q, proceeds)
        st.trades.append((e.trade_date, "SELL", sid, q, gross))
    elif t == "SPLIT":
        sid = _need(e.security_id, "security", e)
        num = _need(e.ratio_num, "ratio_num", e)
        den = _need(e.ratio_den, "ratio_den", e)
        if num <= 0 or den <= 0:
            raise LedgerError("split ratio must be positive")
        pos = st.positions.get(sid)
        if pos:
            r = num / den
            for lot in pos.lots:
                lot.quantity = lot.quantity * r   # basis total unchanged
    elif t == "OPENING_POSITION":
        q = _need(e.quantity, "quantity", e)
        sid = _need(e.security_id, "security", e)
        if q <= 0:
            raise LedgerError("OPENING_POSITION quantity must be > 0")
        pos = st.positions.setdefault(sid, Position(sid))
        pos.lots.append(Lot(e.id, sid, None if e.action_subtype == "UNKNOWN_ACQUIRED" else e.trade_date,
                            q, e.cost_basis_total))
        st.external_flows.append(ExternalFlow(e.trade_date, None, t, sid, q))
    elif t == "CORPORATE_ACTION":
        sid = e.security_id
        if sid and sid in st.positions and st.positions[sid].shares > 0:
            st.positions[sid].frozen_reason = f"{e.action_subtype or 'UNSUPPORTED'} on {e.trade_date}"
        st.issues.append({"type": "UNSUPPORTED_CORPORATE_ACTION", "event_id": e.id, "security_id": sid,
                          "subtype": e.action_subtype, "on": e.trade_date.isoformat()})
    else:
        raise LedgerError(f"unknown event type {t}")


def _consume_lots(st: AccountState, pos: Position, e: LedgerEvent, q: Decimal, proceeds: Decimal) -> None:
    """FIFO lot relief. Proceeds are allocated pro rata by quantity."""
    remaining = q
    per_share = proceeds / q
    # FIFO; lots with unknown acquisition dates are treated as oldest (documented assumption)
    pos.lots.sort(key=lambda l: (l.acquired or date.min, l.lot_id))
    new_lots: list[Lot] = []
    for lot in pos.lots:
        if remaining <= 0:
            new_lots.append(lot)
            continue
        take = min(lot.quantity, remaining)
        cost = None if lot.cost_total is None else lot.cost_total * take / lot.quantity
        lot_proceeds = per_share * take
        if lot.acquired is None:
            hp = "UNKNOWN"
        else:
            hp = "LONG" if e.trade_date > _add_year(lot.acquired) else "SHORT"
        st.realized.append(RealizedGain(e.id, pos.security_id, e.trade_date, take, lot_proceeds, cost,
                                        None if cost is None else lot_proceeds - cost, hp, lot.lot_id))
        left = lot.quantity - take
        remaining -= take
        if left > 0:
            new_lots.append(Lot(lot.lot_id, lot.security_id, lot.acquired, left,
                                None if lot.cost_total is None else lot.cost_total - cost))
    pos.lots = new_lots


def _add_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # Feb 29
        return d + timedelta(days=365)


def realized_total(gains: list[RealizedGain]) -> tuple[Decimal | None, Decimal, int]:
    """(total or None if any component unknown, known subtotal, count of unknown pieces)."""
    known = sum((g.gain for g in gains if g.gain is not None), ZERO)
    unknown = sum(1 for g in gains if g.gain is None)
    return (None if unknown else known), known, unknown
