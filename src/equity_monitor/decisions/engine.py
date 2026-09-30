"""Deterministic recommendation policy engine (pure function; no I/O).

Two separate outputs:
  business assessment: INTACT | WEAKENING | BROKEN | UNKNOWN
  portfolio action:    ADD | HOLD | TRIM | EXIT | REVIEW

Rule precedence (POLICY.md §3):
 1. Missing/stale/conflicting critical evidence, unreconciled holdings, unsupported valuation,
    unapproved thesis or assumptions  -> REVIEW   (verified urgent risks and limit breaches shown separately)
 2. Verified pre-declared invalidation                                     -> EXIT   (ambiguous -> REVIEW)
 3. Issuer or sector limit breach                                          -> TRIM to the limit
 4. Supported thesis + attractive valuation + approved downside + capacity -> ADD
 5. Valuation above the reduction boundary                                 -> TRIM (or EXIT above bull value)
 6. Otherwise                                                               -> HOLD, with the reason

Price alone never decides anything: every price-based rule compares price with an approved,
fact-based value estimate, and a missing input never defaults to HOLD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..config.models import PortfolioPolicy, RecommendationPolicy

ZERO = Decimal(0)


@dataclass
class ConditionState:
    id: str
    description: str
    kind: str
    state: str                  # TRIGGERED | NOT_TRIGGERED | AMBIGUOUS | UNKNOWN
    verified: bool
    assessor: str               # ENGINE | USER | LLM | NONE
    latest_breaches: bool = False


@dataclass
class MilestoneState:
    id: str
    description: str
    state: str                  # MET | MISSED | PENDING


@dataclass
class DecisionInputs:
    security_id: str
    symbol: str
    held: bool
    supported: bool
    unsupported_reason: str | None
    price: Decimal | None
    price_date: date | None
    price_stale_sessions: int | None
    filings_checked_hours_ago: float | None
    filings_check_failed: bool
    financials_overdue: bool
    latest_period_end: date | None
    critical_data_issues: list[str]
    reconciliation_issues: list[str]
    thesis_version_id: str | None
    thesis_next_review: date | None
    conditions: list[ConditionState]
    milestones: list[MilestoneState]
    valuation_id: str | None
    valuation_approved: bool
    downside_reviewed: bool
    valuation_age_days: int | None
    valuation_review_flags: list[str]
    new_financials_since_valuation: bool
    bear: Decimal | None
    base: Decimal | None
    bull: Decimal | None
    base_meaningful: bool
    position_weight: Decimal | None       # issuer-level weight (share classes combined)
    sector: str | None
    sector_weight: Decimal | None
    nav: Decimal | None
    position_value: Decimal | None
    unreviewed_critical_events: list[str] = field(default_factory=list)
    previous_action: str | None = None


@dataclass
class Decision:
    business: str
    action: str
    reason_codes: list[str]
    explanation: str
    missing: list[str]
    urgent: list[str]
    margin_of_safety: Decimal | None
    price_to_base: Decimal | None
    downside: dict
    concentration: dict
    change_conditions: list[str]
    proposed_trade: dict | None


def _business(inp: DecisionInputs) -> tuple[str, list[str]]:
    notes = []
    if inp.thesis_version_id is None or not inp.supported:
        return "UNKNOWN", ["no approved thesis" if inp.thesis_version_id is None else "unsupported business type"]
    if any(c.state == "TRIGGERED" and c.verified for c in inp.conditions):
        return "BROKEN", [f"invalidation met: {c.description}" for c in inp.conditions if c.state == "TRIGGERED" and c.verified]
    if any(c.state == "UNKNOWN" for c in inp.conditions):
        return "UNKNOWN", [f"cannot evaluate: {c.description}" for c in inp.conditions if c.state == "UNKNOWN"]
    weak = [f"possible invalidation: {c.description}" for c in inp.conditions
            if c.state in ("AMBIGUOUS",) or (c.state == "TRIGGERED" and not c.verified)]
    weak += [f"latest period breaches (not yet consecutive): {c.description}" for c in inp.conditions
             if c.state == "NOT_TRIGGERED" and c.latest_breaches]
    weak += [f"milestone missed: {m.description}" for m in inp.milestones if m.state == "MISSED"]
    if weak:
        return "WEAKENING", weak
    return "INTACT", notes


def decide(inp: DecisionInputs, rp: RecommendationPolicy, pp: PortfolioPolicy) -> Decision:
    reasons: list[str] = []
    missing: list[str] = []
    urgent: list[str] = list(inp.unreviewed_critical_events)
    business, bnotes = _business(inp)

    mos = ptb = None
    if inp.price is not None and inp.base is not None and inp.base_meaningful and inp.base > 0:
        mos = 1 - inp.price / inp.base
        ptb = inp.price / inp.base

    conc = {"issuer_weight": inp.position_weight, "issuer_limit": pp.max_issuer_weight, "sector": inp.sector,
            "sector_weight": inp.sector_weight, "sector_limit": pp.max_sector_weight, "nav": inp.nav}
    breach = []
    if inp.held and inp.position_weight is not None and inp.position_weight > pp.max_issuer_weight:
        breach.append("ISSUER_LIMIT_BREACH")
    if inp.held and inp.sector_weight is not None and inp.sector_weight > pp.max_sector_weight:
        breach.append("SECTOR_LIMIT_BREACH")

    downside = {"price": inp.price, "bear_value": inp.bear, "base_value": inp.base, "bull_value": inp.bull,
                "bear_downside": (inp.bear / inp.price - 1) if (inp.bear is not None and inp.price) else None,
                "note": "bear value is a scenario, not a probability-weighted forecast"}

    # ---------------- rule 1: REVIEW gates
    if not inp.supported:
        reasons.append("UNSUPPORTED_VALUATION")
        missing.append(f"valuation framework does not support this business ({inp.unsupported_reason})")
    if inp.reconciliation_issues:
        reasons.append("UNRECONCILED_HOLDING")
        missing += inp.reconciliation_issues
    if inp.critical_data_issues:
        reasons.append("DATA_REFRESH_FAILED")
        missing += inp.critical_data_issues
    if inp.price is None:
        reasons.append("MISSING_PRICE")
        missing.append("no price available")
    elif inp.price_stale_sessions is not None and inp.price_stale_sessions > rp.max_price_age_sessions:
        reasons.append("STALE_PRICE")
        missing.append(f"price is {inp.price_stale_sessions} sessions old (limit {rp.max_price_age_sessions})")
    if inp.filings_check_failed or inp.filings_checked_hours_ago is None \
            or inp.filings_checked_hours_ago > rp.max_filing_check_age_hours:
        reasons.append("FILINGS_NOT_CHECKED")
        missing.append("newer filings have not been checked recently" if not inp.filings_check_failed
                       else "last filings check failed")
    if inp.financials_overdue:
        reasons.append("FINANCIALS_OVERDUE")
        missing.append(f"latest financial period {inp.latest_period_end} is older than {rp.max_financials_age_days} days")
    if inp.thesis_version_id is None and rp.require_thesis_approval:
        reasons.append("NO_APPROVED_THESIS")
        missing.append("no approved thesis")
    if inp.valuation_id is None:
        reasons.append("NO_VALUATION")
        missing.append("no valuation")
    else:
        if rp.require_valuation_approval and not inp.valuation_approved:
            reasons.append("VALUATION_NOT_APPROVED")
            missing.append("valuation assumptions not approved")
        if inp.valuation_review_flags and not inp.valuation_approved:
            missing += inp.valuation_review_flags
        if inp.valuation_age_days is not None and inp.valuation_age_days > rp.max_valuation_age_days:
            reasons.append("VALUATION_STALE")
            missing.append(f"valuation is {inp.valuation_age_days} days old")
        if inp.new_financials_since_valuation:
            reasons.append("NEW_FINANCIALS_SINCE_VALUATION")
            missing.append("financial statements published after the valuation evidence cutoff")
        if not inp.base_meaningful:
            reasons.append("VALUATION_NOT_MEANINGFUL")
            missing.append("base value not positive/meaningful")
    if any(c.state == "UNKNOWN" for c in inp.conditions):
        reasons.append("INVALIDATION_UNEVALUABLE")
        missing += [f"cannot evaluate invalidation condition: {c.description}" for c in inp.conditions if c.state == "UNKNOWN"]
    ambiguous = [c for c in inp.conditions if c.state == "AMBIGUOUS" or (c.state == "TRIGGERED" and not c.verified)]
    if ambiguous:
        reasons.append("AMBIGUOUS_INVALIDATION")
        missing += [f"possible invalidation needs judgment: {c.description} (assessed by {c.assessor})" for c in ambiguous]
    if inp.unreviewed_critical_events:
        reasons.append("UNREVIEWED_CRITICAL_EVENT")
    verified_break = [c for c in inp.conditions if c.state == "TRIGGERED" and c.verified]

    change: list[str] = []
    if reasons:
        if verified_break:
            urgent.append("VERIFIED_INVALIDATION: " + "; ".join(c.description for c in verified_break))
        urgent += breach
        change.append("Resolve: " + "; ".join(missing[:6]) if missing else "Resolve the listed review items")
        return Decision(business, "REVIEW", reasons, "Review required before any action: " + "; ".join(missing[:8]),
                        missing, urgent, mos, ptb, downside, conc, change, None)

    # ---------------- rule 2: verified invalidation
    if verified_break:
        return Decision("BROKEN", "EXIT", ["VERIFIED_THESIS_INVALIDATION"],
                        "Pre-declared invalidation condition met and verified: " + "; ".join(c.description for c in verified_break),
                        missing, urgent, mos, ptb, downside, conc,
                        ["Owner may override with a documented rationale; the original thesis stays on record."],
                        {"side": "SELL", "target_weight": ZERO, "amount": inp.position_value} if inp.held else None)

    # ---------------- rule 3: limit breach
    if breach:
        target = pp.max_issuer_weight if "ISSUER_LIMIT_BREACH" in breach else None
        trade = None
        if inp.nav and inp.position_weight is not None:
            excess_w = inp.position_weight - pp.max_issuer_weight if "ISSUER_LIMIT_BREACH" in breach else \
                inp.sector_weight - pp.max_sector_weight  # type: ignore[operator]
            trade = {"side": "SELL", "target_weight": target, "amount": max(ZERO, excess_w) * inp.nav,
                     "note": "reduce to the configured limit; prefer new contributions elsewhere if they fix it"}
        return Decision(business, "TRIM", breach, f"Concentration above configured limit ({', '.join(breach)}).",
                        missing, urgent, mos, ptb, downside, conc,
                        ["Weight back within limits (by growth of the rest of the portfolio or a trim)."], trade)

    add_threshold = rp.add_exit_margin_of_safety if inp.previous_action == "ADD" else rp.add_min_margin_of_safety
    trim_threshold = rp.trim_release_price_to_base if inp.previous_action == "TRIM" else rp.trim_price_to_base
    assert mos is not None and ptb is not None and inp.price is not None and inp.base is not None

    # ---------------- rule 4: ADD eligibility
    add_blockers = []
    if business != "INTACT":
        add_blockers.append(f"business assessment {business}: " + "; ".join(bnotes))
    if mos < add_threshold:
        add_blockers.append(f"margin of safety {mos:.1%} < {add_threshold:.0%}"
                            + (" (hysteresis exit band)" if inp.previous_action == "ADD" else ""))
    if rp.require_downside_review and not inp.downside_reviewed:
        add_blockers.append("downside scenario not reviewed")
    if inp.bear is None or inp.bear < inp.price * (1 - rp.max_bear_downside):
        add_blockers.append(f"bear-case downside exceeds {rp.max_bear_downside:.0%}")
    w = inp.position_weight or ZERO
    if w >= min(pp.target_position_weight, pp.max_issuer_weight):
        add_blockers.append(f"position at/above target weight {pp.target_position_weight:.0%}")
    if inp.sector_weight is not None and inp.sector_weight >= pp.max_sector_weight:
        add_blockers.append(f"sector {inp.sector} at/above limit {pp.max_sector_weight:.0%}")
    add_price = inp.base * (1 - rp.add_min_margin_of_safety)
    trim_price = inp.base * rp.trim_price_to_base
    change = [f"ADD band starts at price <= {add_price:.2f} (MoS {rp.add_min_margin_of_safety:.0%}) if thesis stays intact",
              f"TRIM considered at price >= {trim_price:.2f} ({rp.trim_price_to_base:.0%} of base value)"]
    if inp.bull is not None and rp.exit_price_to_bull is not None:
        change.append(f"EXIT considered at price >= {inp.bull * rp.exit_price_to_bull:.2f} (bull value)")
    change += [f"EXIT if verified: {c.description}" for c in inp.conditions]
    if not add_blockers:
        return Decision(business, "ADD", ["THESIS_INTACT", "VALUATION_ATTRACTIVE", "CAPACITY_AVAILABLE"],
                        f"Thesis intact; margin of safety {mos:.1%} >= {add_threshold:.0%}; within limits.",
                        missing, urgent, mos, ptb, downside, conc, change, None)

    # ---------------- rule 5: valuation reduction boundary (only meaningful for a held position)
    if inp.held:
        if rp.exit_price_to_bull is not None and inp.bull is not None and inp.bull > 0 \
                and inp.price > inp.bull * rp.exit_price_to_bull:
            return Decision(business, "EXIT", ["PRICE_ABOVE_BULL_VALUE"],
                            f"Price {inp.price} exceeds the bull-case value {inp.bull:.2f}: ownership case unattractive under policy.",
                            missing, urgent, mos, ptb, downside, conc, change,
                            {"side": "SELL", "target_weight": ZERO, "amount": inp.position_value})
        if ptb > trim_threshold:
            target_w = min(w, pp.target_position_weight) / 2 if w else ZERO
            return Decision(business, "TRIM", ["VALUATION_ABOVE_TRIM_BAND"],
                            f"Price is {ptb:.0%} of base value (> {trim_threshold:.0%}"
                            + (" hysteresis release band" if inp.previous_action == "TRIM" else "") + ").",
                            missing, urgent, mos, ptb, downside, conc, change,
                            {"side": "SELL", "target_weight": target_w,
                             "amount": (w - target_w) * inp.nav if inp.nav else None,
                             "note": "illustrative trim to half the target weight; owner decides size"})

    # ---------------- rule 6: HOLD
    reasons = ["NO_ADD"] + (["NOT_HELD"] if not inp.held else [])
    return Decision(business, "HOLD", reasons,
                    ("Ownership remains reasonable but new money is not preferred: " if inp.held else
                     "Not an ADD candidate now: ") + "; ".join(add_blockers),
                    missing, urgent, mos, ptb, downside, conc, change, None)
