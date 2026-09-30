"""Paper execution of recommendations into a PAPER portfolio (prospective only).

Rule: a recommendation created at time T fills at the OPEN of the first session whose open is
strictly after T, plus configured slippage and fees. Never at a price observed before T. If that
bar is not available yet, the fill is pending. Policy performance (paper) is kept separate from the
owner's actual execution.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from ..app import App
from ..data import calendar as cal
from ..data.prices import bar_on
from ..db.core import insert, one
from ..ledger.store import NewEvent, accounts_of, portfolio_kind, record_events
from ..ledger.views import portfolio_view
from ..util import D, dstr, new_id, parse_utc


class PaperError(ValueError):
    pass


def fill_session(created_at_iso: str):
    created = parse_utc(created_at_iso)
    d = cal.ny_date(created)
    d = cal.session_on_or_after(d)
    while cal.session_open_utc(d) <= created:
        d = cal.next_session(d)
    return d


def paper_execute(app: App, recommendation_id: str, paper_portfolio_id: str, *, notional: Decimal = Decimal(1000)) -> str | None:
    if portfolio_kind(app, paper_portfolio_id) != "PAPER":
        raise PaperError("paper executions may only be recorded into a PAPER portfolio")
    if app.policy.status != "FROZEN":
        raise PaperError("freeze the paper-execution policy first (policy status FROZEN) so results are prospective")
    if one(app.conn, "SELECT 1 FROM paper_execution WHERE recommendation_id=?", (recommendation_id,)):
        return None
    rec = one(app.conn, "SELECT * FROM recommendation WHERE id=?", (recommendation_id,))
    if rec["action"] not in ("ADD", "TRIM", "EXIT"):
        return None
    d = fill_session(rec["created_at"])
    bar = bar_on(app, rec["security_id"], d)
    if bar is None or bar["open"] is None:
        return None      # pending until the executable bar exists
    slip = app.policy.paper.slippage_bps / Decimal(10000)
    acct = accounts_of(app, paper_portfolio_id)[0]["id"]
    view = portfolio_view(app, paper_portfolio_id, d)
    h = view.holding(rec["security_id"])
    if rec["action"] == "ADD":
        px = D(bar["open"]) * (1 + slip)
        qty = (notional / px).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        side = "BUY"
    else:
        if h is None or h.shares <= 0:
            return None
        px = D(bar["open"]) * (1 - slip)
        qty = h.shares if rec["action"] == "EXIT" else (h.shares / 2).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        side = "SELL"
    fee = app.policy.paper.fee_per_trade_usd
    res = record_events(app, acct, [NewEvent(side, d, rec["security_id"], quantity=qty, price=px.quantize(Decimal("0.0001")),
                                             fees=fee, external_id=f"paper:{recommendation_id}",
                                             note=f"paper fill of {rec['action']} at next open")], recorded_by="paper")
    if res.rejected:
        raise PaperError(str(res.rejected))
    pid = new_id("pex")
    insert(app.conn, "paper_execution", {"id": pid, "recommendation_id": recommendation_id, "paper_portfolio_id": paper_portfolio_id,
                                         "fill_session_date": d.isoformat(), "fill_price": dstr(px), "quantity": dstr(qty),
                                         "side": side, "fees": dstr(fee), "slippage_bps": dstr(app.policy.paper.slippage_bps),
                                         "policy_version_id": app.policy_version_id(), "created_at": app.now_iso()})
    return pid
