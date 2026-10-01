"""Current-conditions assessment -> purchase eligibility (ELIGIBLE | PAUSED | BLOCKED). Pure function.

Linked to, but separate from, the long-term action (ADD/HOLD/TRIM/EXIT/REVIEW):
- BLOCKED: hard constraints — long-term action REVIEW/TRIM/EXIT, business BROKEN, unsupported valuation,
  issuer/sector limit reached, unreconciled cash. Market context can never lift a block.
- PAUSED: no hard block, but current conditions say wait (unreviewed material event, adverse development
  on a HIGH exposure, UNKNOWN conditions for a HIGH exposure, missing approved exposure profile,
  owner-opted market-stress pause). Every pause has a reason, sources, a reassessment condition and date.
- ELIGIBLE: nothing blocks or pauses purchases. The allocator buys only when action == ADD AND ELIGIBLE,
  so a HOLD can be ELIGIBLE (conditions allow) yet receive no new money because the long-term case
  does not prefer it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..config.models import MarketPolicy, PortfolioPolicy
from .engine import Decision, DecisionInputs


@dataclass
class Pause:
    code: str
    detail: str
    reassess_condition: str
    reassess_on: date | None
    cluster: str | None = None
    sources: list[dict] = field(default_factory=list)


@dataclass
class CurrentConditions:
    eligibility: str
    blocks: list[str]
    pauses: list[Pause]
    research: list[dict]
    unknowns: list[str]
    context_count: int
    independent_developments: int
    observations: int

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def assess(inp: DecisionInputs, d: Decision, pp: PortfolioPolicy, mp: MarketPolicy, impacts, *, has_profile: bool,
           cash_unreconciled: bool, today: date, profile_evidence: tuple[str, list[str]] = ("OK", [])) -> CurrentConditions:
    blocks: list[str] = []
    if d.action in ("REVIEW", "TRIM", "EXIT"):
        blocks.append(f"long-term action is {d.action}")
    if d.business == "BROKEN":
        blocks.append("thesis invalidated (BROKEN)")
    if not inp.supported:
        blocks.append(f"valuation framework unsupported ({inp.unsupported_reason})")
    if inp.position_weight is not None and inp.position_weight >= pp.max_issuer_weight:
        blocks.append(f"issuer weight {inp.position_weight:.1%} at/above limit {pp.max_issuer_weight:.0%}")
    if inp.sector_weight is not None and inp.sector_weight >= pp.max_sector_weight:
        blocks.append(f"sector weight {inp.sector_weight:.1%} at/above limit {pp.max_sector_weight:.0%}")
    if cash_unreconciled:
        blocks.append("cash history unreconciled")
    pauses: list[Pause] = []
    research, unknowns, ctx, n_obs, n_dev = [], [], 0, 0, 0
    if mp.enabled and impacts is not None:
        n_obs = len(impacts.observations)
        n_dev = len(impacts.clusters)
        unknowns = list(impacts.unknowns)
        for c in impacts.chains:
            if c.effect == "PAUSE_PURCHASES":
                pauses.append(Pause(c.reason, c.mechanism, c.reassess_condition or "owner review",
                                    c.reassess_on, c.cluster_key, c.sources))
            elif c.effect == "RESEARCH_TASK":
                research.append({"cluster": c.cluster_key, "reason": c.reason, "detail": c.mechanism,
                                 "next_step": c.reassess_condition})
            else:
                ctx += 1
        if not has_profile and mp.require_exposure_profile_for_purchase:
            pauses.append(Pause("NO_APPROVED_EXPOSURE_PROFILE",
                                "economic/sector sensitivities are UNKNOWN without an approved exposure profile",
                                "approve an exposure profile (`eqm exposure draft/approve`)",
                                today + timedelta(days=mp.pause_reassess_days)))
        elif has_profile and profile_evidence[0] != "OK" and mp.require_exposure_profile_for_purchase:
            # an approval of evidence the current verifier rejects or has not resolved cannot support purchases
            failed = profile_evidence[0] == "FAILED"
            pauses.append(Pause("EXPOSURE_EVIDENCE_FAILED" if failed else "EXPOSURE_EVIDENCE_REVIEW_REQUIRED",
                                "approved exposure profile evidence " + ("fails" if failed else "is unresolved under")
                                + " the current verifier: " + "; ".join(profile_evidence[1]),
                                "create a corrected exposure profile" if failed else
                                "review the evidence and re-approve (`eqm exposure approve --version-id ... "
                                "--acknowledge-unverified`)", today + timedelta(days=mp.pause_reassess_days)))
    eligibility = "BLOCKED" if blocks else ("PAUSED" if pauses else "ELIGIBLE")
    return CurrentConditions(eligibility, blocks, pauses, research, unknowns, ctx, n_dev, n_obs)


def baseline(inp: DecisionInputs, d: Decision, pp: PortfolioPolicy, cash_unreconciled: bool, today: date) -> str:
    """Fundamental-only baseline: hard constraints only, no market/sector/company-condition pauses."""
    return assess(inp, d, pp, MarketPolicy(enabled=False), None, has_profile=True, cash_unreconciled=cash_unreconciled,
                  today=today).eligibility
