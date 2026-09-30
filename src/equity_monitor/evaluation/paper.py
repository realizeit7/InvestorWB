"""Paper execution into a PAPER portfolio (prospective only; never borrows).

Rules:
- A decision created at time T fills at the OPEN of the first session whose open is strictly after T, plus
  configured slippage and fees. Never at a price observed before T. If a needed bar is missing, nothing is recorded
  (pending) — fills are all-or-nothing.
- Execution is bound to the ORIGINATING decision's policy: that policy version must be FROZEN and must equal the
  active policy. Provenance (proposal, recommendation ids, policy version) is stored with every fill.
- Purchases go only through an allocation proposal (``paper_execute_allocation``). Each line is re-checked against
  its variant's eligibility (augmented: ``purchase_eligibility``; baseline: ``baseline_eligibility``), the paper
  book's available cash (fees included; no negative cash, ever), the fractional-share setting, the minimum trade and
  the issuer/sector limits measured on the paper book.
- ``paper_execute`` executes only TRIM (sell down to the recommendation's documented ``target_weight``; for a sector
  breach, the amount needed to meet the sector limit) and EXIT (sell everything).
- Each (proposal, variant, paper portfolio) and each recommendation executes at most once; ledger rows and the
  execution record are written in one transaction.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from ..app import App
from ..data import calendar as cal
from ..data.prices import bar_on
from ..data.securities import find_security, security_ref
from ..db.core import insert, one, transaction
from ..ledger.store import NewEvent, accounts_of, portfolio_kind, record_events
from ..ledger.views import portfolio_view
from ..util import D, dstr, from_json, new_id, parse_utc, to_json

ZERO = Decimal(0)


class PaperError(ValueError):
    pass


def fill_session(created_at_iso: str):
    created = parse_utc(created_at_iso)
    d = cal.ny_date(created)
    d = cal.session_on_or_after(d)
    while cal.session_open_utc(d) <= created:
        d = cal.next_session(d)
    return d


def _bind_policy(app: App, origin_policy_id: str | None, what: str) -> str:
    row = one(app.conn, "SELECT id, status FROM policy_version WHERE id=?", (origin_policy_id,)) if origin_policy_id else None
    if row is None or row["status"] != "FROZEN":
        raise PaperError(f"the {what} was not made under a FROZEN policy; freeze the policy before the decision so "
                         "paper results are prospective")
    if app.policy_version_id() != origin_policy_id:
        raise PaperError(f"the active policy differs from the FROZEN policy the {what} was made under "
                         f"({origin_policy_id}); paper execution uses the originating policy only")
    return origin_policy_id


def _book(app: App, paper_portfolio_id: str):
    if portfolio_kind(app, paper_portfolio_id) != "PAPER":
        raise PaperError("paper executions may only be recorded into a PAPER portfolio")
    accts = accounts_of(app, paper_portfolio_id)
    if not accts:
        raise PaperError("the paper portfolio has no account")
    return accts[0]["id"]


def _size(qty: Decimal, fractional: bool, rounding=ROUND_DOWN) -> Decimal:
    return qty.to_integral_value(rounding=rounding) if not fractional else qty.quantize(Decimal("0.000001"), rounding=rounding)


def paper_execute(app: App, recommendation_id: str, paper_portfolio_id: str) -> str | None:
    """Execute a TRIM or EXIT recommendation in a paper book. Returns the execution id, or None when there is
    nothing to do (HOLD/REVIEW, no position, already executed, fill bar not available yet)."""
    acct = _book(app, paper_portfolio_id)
    rec = one(app.conn, "SELECT * FROM recommendation WHERE id=?", (recommendation_id,))
    if rec is None:
        raise PaperError(f"unknown recommendation {recommendation_id}")
    if rec["action"] == "ADD":
        raise PaperError("ADD is paper-executed only through an allocation proposal (paper_execute_allocation), "
                         "which applies cash, limits, fees and eligibility")
    if rec["action"] not in ("TRIM", "EXIT"):
        return None
    policy_id = _bind_policy(app, rec["policy_version_id"], "recommendation")
    if one(app.conn, "SELECT 1 FROM paper_execution WHERE recommendation_id=?", (recommendation_id,)):
        return None
    d = fill_session(rec["created_at"])
    bar = bar_on(app, rec["security_id"], d)
    if bar is None or bar["open"] is None:
        return None      # pending until the executable bar exists
    pol, paper = app.policy.portfolio, app.policy.paper
    slip = paper.slippage_bps / Decimal(10000)
    px = D(bar["open"]) * (1 - slip)
    view = portfolio_view(app, paper_portfolio_id, cal.previous_session(d))   # information known before the open
    h = view.holding(rec["security_id"])
    if h is None or h.shares <= 0:
        return None
    if rec["action"] == "EXIT":
        qty, basis = h.shares, "EXIT: sell the whole position"
    else:
        trade = (from_json(rec["payload_json"]).get("proposed_trade") or {})
        ref = security_ref(app.conn, rec["security_id"])
        # value the book with this security at the fill price
        nav = (view.nav or ZERO) - (h.market_value or ZERO) + h.shares * px
        issuer_val = (view.issuer_weights.get(ref.issuer_id or rec["security_id"], ZERO) * (view.nav or ZERO)
                      - (h.market_value or ZERO) + h.shares * px)
        if trade.get("target_weight") is not None:
            tw = D(trade["target_weight"])
            sell_value = issuer_val - tw * nav
            basis = f"TRIM to documented target weight {tw}"
        else:
            sector_val = view.sector_weights.get(ref.sector or "Unknown", ZERO) * (view.nav or ZERO) \
                - (h.market_value or ZERO) + h.shares * px
            sell_value = sector_val - pol.max_sector_weight * nav
            basis = f"TRIM to sector limit {pol.max_sector_weight}"
        if view.nav is None or sell_value <= 0:
            return None
        qty = min(h.shares, _size(sell_value / px, pol.fractional_shares, ROUND_UP))
    fee = paper.fee_per_trade_usd
    with transaction(app.conn):
        res = record_events(app, acct, [NewEvent("SELL", d, rec["security_id"], quantity=qty,
                                                 price=px.quantize(Decimal("0.0001")), fees=fee,
                                                 external_id=f"paper:{recommendation_id}",
                                                 note=f"paper fill of {rec['action']} at next open ({basis})")],
                            recorded_by="paper", allow_negative_cash=False)
        if res.rejected:
            raise PaperError(str(res.rejected))
        if not res.inserted:
            return None
        pid = new_id("pex")
        insert(app.conn, "paper_execution", {"id": pid, "recommendation_id": recommendation_id,
                                             "paper_portfolio_id": paper_portfolio_id, "fill_session_date": d.isoformat(),
                                             "fill_price": dstr(px), "quantity": dstr(qty), "side": "SELL",
                                             "fees": dstr(fee), "slippage_bps": dstr(paper.slippage_bps),
                                             "policy_version_id": policy_id, "created_at": app.now_iso()})
    return pid


def paper_execute_allocation(app: App, proposal_id: str, paper_portfolio_id: str, variant: str) -> list[str]:
    """Fill one variant ('augmented' or 'baseline') of an allocation proposal in a PAPER portfolio at the next open
    after the proposal was created. Fund both paper portfolios with the same deposits so the variants can be
    compared. Returns the symbols bought; repeated calls return the recorded result without new fills."""
    if variant not in ("augmented", "baseline"):
        raise PaperError("variant must be 'augmented' or 'baseline'")
    acct = _book(app, paper_portfolio_id)
    prop = one(app.conn, "SELECT * FROM allocation_proposal WHERE id=?", (proposal_id,))
    if prop is None:
        raise PaperError(f"unknown proposal {proposal_id}")
    policy_id = _bind_policy(app, prop["policy_version_id"], "allocation proposal")
    done = one(app.conn, "SELECT fills_json FROM paper_allocation_execution WHERE proposal_id=? AND variant=? AND "
                         "paper_portfolio_id=?", (proposal_id, variant, paper_portfolio_id))
    if done:
        return [f["symbol"] for f in from_json(done["fills_json"])]
    payload = from_json(prop["payload_json"])
    raw = [l for l in payload["lines"] if D(l["amount"]) > 0] if variant == "augmented" else payload["baseline"]["lines"]
    if any(not l.get("recommendation_id") for l in raw):
        raise PaperError("proposal lines lack recommendation provenance; create a new proposal")
    elig_col = "purchase_eligibility" if variant == "augmented" else "baseline_eligibility"
    d = fill_session(prop["created_at"])
    pol, paper = app.policy.portfolio, app.policy.paper
    slip = paper.slippage_bps / Decimal(10000)
    view = portfolio_view(app, paper_portfolio_id, cal.previous_session(d))
    nav = view.nav if view.nav is not None else view.cash
    cash_left = view.available_cash
    issuer_add: dict[str, Decimal] = {}
    sector_add: dict[str, Decimal] = {}
    events, fills, skipped = [], [], []
    for l in raw:
        sym, amount = l["symbol"], D(l["amount"])
        sid = find_security(app.conn, sym)
        rec = one(app.conn, "SELECT action, policy_version_id, purchase_eligibility, baseline_eligibility, as_of "
                            "FROM recommendation WHERE id=?", (l["recommendation_id"],))
        if rec is None or rec["action"] != "ADD" or rec[elig_col] != "ELIGIBLE":
            skipped.append({"symbol": sym, "reason": f"{variant} rule: needs ADD with {elig_col}=ELIGIBLE, got "
                            f"{rec['action'] if rec else None}/{rec[elig_col] if rec else None}"})
            continue
        if rec["policy_version_id"] != policy_id or rec["as_of"] > prop["as_of"]:
            skipped.append({"symbol": sym, "reason": "recommendation not validated under the proposal's policy/cutoff"})
            continue
        bar = bar_on(app, sid, d)
        if bar is None or bar["open"] is None:
            return []     # pending: fills are all-or-nothing, nothing recorded
        px = (D(bar["open"]) * (1 + slip)).quantize(Decimal("0.0001"))
        ref = security_ref(app.conn, sid)
        ik, sk = ref.issuer_id or sid, ref.sector or "Unknown"
        rooms = {"PROPOSED_AMOUNT": amount, "CASH": cash_left - paper.fee_per_trade_usd}
        if nav and nav > 0:
            rooms["ISSUER_LIMIT"] = pol.max_issuer_weight * nav - view.issuer_weights.get(ik, ZERO) * nav - issuer_add.get(ik, ZERO)
            rooms["SECTOR_LIMIT"] = pol.max_sector_weight * nav - view.sector_weights.get(sk, ZERO) * nav - sector_add.get(sk, ZERO)
        bind = min(rooms, key=lambda k: rooms[k])
        qty = _size(max(ZERO, rooms[bind]) / px, pol.fractional_shares)
        cost = qty * px
        if qty <= 0 or cost < pol.min_trade_usd:
            skipped.append({"symbol": sym, "reason": f"{bind} leaves {max(ZERO, rooms[bind]):.2f}: below the minimum "
                            f"trade or one share"})
            continue
        cash_left -= cost + paper.fee_per_trade_usd
        issuer_add[ik] = issuer_add.get(ik, ZERO) + cost
        sector_add[sk] = sector_add.get(sk, ZERO) + cost
        events.append(NewEvent("BUY", d, sid, quantity=qty, price=px, fees=paper.fee_per_trade_usd,
                               external_id=f"paper:{proposal_id}:{variant}:{sym}",
                               note=f"paper {variant} allocation fill at next open (proposal {proposal_id})"))
        fills.append({"symbol": sym, "recommendation_id": l["recommendation_id"], "quantity": qty, "price": px,
                      "fee": paper.fee_per_trade_usd, "amount": cost, "proposed_amount": amount, "binding": bind})
    with transaction(app.conn):
        res = record_events(app, acct, events, recorded_by="paper", allow_negative_cash=False) if events else None
        if res is not None and (res.rejected or res.duplicates):
            raise PaperError(f"paper fills rejected (nothing recorded): {res.rejected or 'duplicate fills'}")
        insert(app.conn, "paper_allocation_execution", {
            "id": new_id("pax"), "proposal_id": proposal_id, "variant": variant, "paper_portfolio_id": paper_portfolio_id,
            "fill_session_date": d.isoformat(), "policy_version_id": policy_id, "fills_json": to_json(fills),
            "skipped_json": to_json(skipped), "ledger_event_ids_json": to_json(res.inserted if res else []),
            "created_at": app.now_iso()})
    return [f["symbol"] for f in fills]
