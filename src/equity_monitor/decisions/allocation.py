"""Deterministic monthly contribution allocator.

Algorithm (POLICY.md §5):
 1. Candidates = holdings + APPROVED watchlist names whose latest recommendation (<= 7 days old)
    is ADD. ETFs are excluded.
 2. Rank by base-case margin of safety (desc), then latest screen score (desc, missing last),
    then symbol (asc) as the final deterministic tie-breaker.
 3. For each candidate allocate min(room to target weight, room to issuer limit, room to sector
    limit, remaining budget - fee), measured against post-contribution NAV (cash included).
 4. Skip amounts below the minimum trade size; round down to whole shares if fractional shares
    are not supported.
 5. Whatever is left stays as cash. Nothing forces the full budget to be spent.

Proposals never modify holdings. Planned contributions are not cash until recorded; a hypothetical
amount is labelled HYPOTHETICAL. Sale proceeds are not counted unless explicitly requested and
labelled conditional.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from ..app import App
from ..data import calendar as cal
from ..data.prices import price_on_or_before
from ..data.securities import security_ref
from ..db.core import all_rows, insert, one
from ..ledger.views import portfolio_view
from ..util import dstr, from_json, iso_utc, new_id, parse_utc, to_json
from .recommend import is_preview, latest_for

ZERO = Decimal(0)
MAX_REC_AGE = timedelta(days=7)


@dataclass
class Line:
    security_id: str
    symbol: str
    recommendation_id: str
    rank: int
    margin_of_safety: Decimal | None
    screen_score: Decimal | None
    price: Decimal
    current_value: Decimal
    current_weight: Decimal
    amount: Decimal = ZERO
    shares: Decimal = ZERO
    fee: Decimal = ZERO
    proposed_weight: Decimal = ZERO
    binding: str | None = None
    note: str = ""


@dataclass
class Proposal:
    id: str
    portfolio_id: str
    as_of: str
    kind: str
    label: str
    is_preview: bool
    budget: Decimal
    deployable_cash: Decimal
    nav_before: Decimal | None
    nav_after: Decimal | None
    lines: list[Line]
    excluded: list[dict]
    remaining_cash: Decimal
    binding_constraints: list[str]
    notes: list[str] = field(default_factory=list)
    baseline: dict = field(default_factory=dict)


def _screen_score(app: App, security_id: str) -> Decimal | None:
    r = one(app.conn, "SELECT r.score FROM screening_result r JOIN screening_run s ON s.id=r.run_id WHERE r.security_id=? "
                      "ORDER BY s.created_at DESC LIMIT 1", (security_id,))
    return Decimal(r["score"]) if r and r["score"] is not None else None


def propose(app: App, portfolio_id: str, *, as_of: datetime | None = None, hypothetical_contribution: Decimal | None = None,
            conditional_sale_proceeds: Decimal | None = None) -> Proposal:
    as_of = as_of or app.now()
    pol = app.policy.portfolio
    session = cal.latest_completed_session(as_of)
    view = portfolio_view(app, portfolio_id, session)
    kind = "HYPOTHETICAL" if hypothetical_contribution else "CONFIRMED"
    notes: list[str] = []
    excluded: list[dict] = []
    binding: list[str] = []
    deployable = max(ZERO, view.available_cash - pol.cash_buffer_usd)
    if view.unsettled_cash:
        notes.append(f"{view.unsettled_cash:.2f} of cash is unsettled and not deployable yet")
    budget = deployable + (hypothetical_contribution or ZERO)
    if conditional_sale_proceeds:
        budget += conditional_sale_proceeds
        notes.append(f"includes CONDITIONAL sale proceeds of {conditional_sale_proceeds:.2f}; valid only if the sale is executed")
    if not app.settings.risk.outside_holdings_known:
        notes.append("Household-level concentration is UNKNOWN: retirement/outside holdings are not recorded.")
    if app.settings.contribution.schg_relationship == "undecided":
        notes.append("Whether this contribution replaces or supplements SCHG purchases is undecided (setting).")
    lines: list[Line] = []
    nav_after = None if view.nav is None else view.nav + (hypothetical_contribution or ZERO)
    if view.nav is None:
        binding.append("NAV_UNKNOWN")
        notes.append("Missing prices for: " + ", ".join(view.missing_prices) + "; weights cannot be computed.")
    if any(i["issue_type"] == "NEGATIVE_CASH" for i in view.open_issues):
        binding.append("CASH_RECONCILIATION_OPEN")
        notes.append("Cash history is unreconciled (went negative); allocation withheld until resolved.")
    blocked = bool(binding)

    # candidates
    held_ids = {h.security_id for h in view.holdings if h.security_type not in ("ETF", "FUND")}
    wl = {r["security_id"] for r in all_rows(app.conn, "SELECT security_id FROM watchlist_entry WHERE status='APPROVED'")}
    cands = []
    for sid in sorted(held_ids | wl):
        rec = latest_for(app, portfolio_id, sid)
        ref = security_ref(app.conn, sid)
        if rec is None:
            excluded.append({"symbol": ref.symbol, "reason": "no recommendation"})
            continue
        if as_of - parse_utc(rec["as_of"]) > MAX_REC_AGE:
            excluded.append({"symbol": ref.symbol, "reason": f"recommendation older than {MAX_REC_AGE.days} days"})
            continue
        if rec["action"] != "ADD":
            excluded.append({"symbol": ref.symbol, "reason": f"latest action {rec['action']}"})
            continue
        px = price_on_or_before(app, sid, session)
        if px is None:
            excluded.append({"symbol": ref.symbol, "reason": "no price"})
            continue
        mos = rec["payload"].get("margin_of_safety")
        cands.append((sid, ref, rec, px[1], Decimal(mos) if mos is not None else None, _screen_score(app, sid)))
    cands.sort(key=lambda c: (-(c[4] if c[4] is not None else Decimal(-9)), -(c[5] if c[5] is not None else Decimal(-9)),
                              c[1].symbol))

    def fill(selected) -> tuple[list[Line], Decimal, list[str]]:
        """Deterministic fill. Eligibility only removes candidates; limits are applied identically."""
        lines_: list[Line] = []
        binding_: list[str] = list(binding)
        remaining = budget
        sector_alloc: dict[str, Decimal] = {}
        n_holdings = len(held_ids)
        for rank, (sid, ref, rec, price, mos, score) in enumerate(selected, 1):
            h = view.holding(sid)
            cur = h.market_value if h and h.market_value is not None else ZERO
            issuer_value = (view.issuer_weights.get(ref.issuer_id or sid, ZERO) * view.nav) if view.nav else cur
            line = Line(sid, ref.symbol, rec["id"], rank, mos, score, price, cur,
                        (issuer_value / view.nav) if view.nav else ZERO)
            lines_.append(line)
            if blocked or nav_after is None or nav_after <= 0:
                line.binding = "BLOCKED"
                continue
            if sid not in held_ids and n_holdings >= pol.target_holdings_max:
                line.binding, line.note = "MAX_HOLDINGS", f"already at {pol.target_holdings_max} holdings"
                binding_.append("MAX_HOLDINGS")
                continue
            sector = ref.sector or "Unknown"
            sector_value = view.sector_weights.get(sector, ZERO) * view.nav + sector_alloc.get(sector, ZERO)
            rooms = {
                "TARGET_WEIGHT": pol.target_position_weight * nav_after - issuer_value,
                "ISSUER_LIMIT": pol.max_issuer_weight * nav_after - issuer_value,
                "SECTOR_LIMIT": pol.max_sector_weight * nav_after - sector_value,
                "BUDGET": remaining - pol.fee_per_trade_usd,
            }
            constraint = min(rooms, key=lambda k: (rooms[k], list(rooms).index(k)))
            amount = max(ZERO, rooms[constraint])
            if not pol.fractional_shares:
                shares = (amount / price).to_integral_value(rounding=ROUND_DOWN)
                amount = shares * price
                if shares == 0 and rooms[constraint] > 0:
                    constraint = "WHOLE_SHARE_ROUNDING"
            else:
                shares = (amount / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                amount = (shares * price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if amount < pol.min_trade_usd:
                line.binding = constraint if amount <= 0 else "MIN_TRADE_SIZE"
                line.note = f"allocation {amount:.2f} below minimum trade {pol.min_trade_usd}" if amount > 0 else "no room"
                binding_.append(line.binding)
                continue
            line.amount, line.shares, line.fee, line.binding = amount, shares, pol.fee_per_trade_usd, constraint
            line.proposed_weight = (issuer_value + amount) / nav_after
            remaining -= amount + pol.fee_per_trade_usd
            sector_alloc[sector] = sector_alloc.get(sector, ZERO) + amount
            if sid not in held_ids:
                n_holdings += 1
            binding_.append(constraint)
        return lines_, remaining, binding_

    eligible = []
    for c in cands:
        el = c[2].get("purchase_eligibility")
        if el == "ELIGIBLE":
            eligible.append(c)
        else:
            cc = c[2]["payload"].get("current_conditions") or {}
            why = "; ".join([p["code"] + (f" (reassess {p['reassess_on']})" if p.get("reassess_on") else "")
                             for p in cc.get("pauses", [])] + cc.get("blocks", [])) or "eligibility unknown: re-run review"
            excluded.append({"symbol": c[1].symbol, "reason": f"ADD but purchases {el or 'UNKNOWN'}: {why}"})
    lines, remaining, binding = fill(eligible)
    b_lines, b_remaining, _ = fill([c for c in cands if c[2].get("baseline_eligibility") == "ELIGIBLE"])
    baseline_summary = {"lines": [{"symbol": l.symbol, "amount": l.amount} for l in b_lines if l.amount > 0],
                        "remaining_cash": b_remaining, "cash_withheld_vs_baseline": remaining - b_remaining,
                        "note": "fundamental-only baseline (no market/sector/company-condition pauses), same limits"}
    if not eligible:
        notes.append("No ADD-eligible candidates: the full budget stays as cash. That is an acceptable outcome.")
    if remaining > 0 and eligible and not blocked:
        notes.append(f"{remaining:.2f} left unallocated because constraints bound ({', '.join(sorted(set(binding)))}).")
    pid = new_id("alloc")
    prop = Proposal(pid, portfolio_id, iso_utc(as_of), kind, view.kind, is_preview(app, portfolio_id), budget, deployable,
                    view.nav, nav_after, lines, excluded, remaining, sorted(set(binding)), notes, baseline_summary)
    insert(app.conn, "allocation_proposal", {
        "id": pid, "portfolio_id": portfolio_id, "as_of": iso_utc(as_of), "contribution_kind": kind, "budget": dstr(budget),
        "payload_json": to_json(asdict(prop)), "remaining_cash": dstr(remaining), "policy_version_id": app.policy_version_id(),
        "is_preview": int(prop.is_preview), "label": view.kind, "created_at": app.now_iso(),
    })
    app.audit("allocation.proposed", "allocation_proposal", pid, {"budget": str(budget), "remaining": str(remaining)})
    return prop


def load(app: App, proposal_id: str) -> dict:
    r = one(app.conn, "SELECT * FROM allocation_proposal WHERE id=?", (proposal_id,))
    return {**dict(r), "payload": from_json(r["payload_json"])}


def latest(app: App, portfolio_id: str) -> dict | None:
    r = one(app.conn, "SELECT id FROM allocation_proposal WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 1", (portfolio_id,))
    return load(app, r["id"]) if r else None
