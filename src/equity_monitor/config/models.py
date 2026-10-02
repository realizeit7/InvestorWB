"""Typed, versioned configuration.

``Policy`` holds every decision threshold. All defaults are PROVISIONAL engineering choices,
not validated investment rules (see POLICY.md). ``UserSettings`` holds things only the owner
can supply; unknown values stay ``None`` and force preview labelling.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..util import stable_hash


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ policy
class RecommendationPolicy(_Strict):
    add_min_margin_of_safety: Decimal = Decimal("0.25")     # enter ADD band
    add_exit_margin_of_safety: Decimal = Decimal("0.20")    # leave ADD band (hysteresis)
    trim_price_to_base: Decimal = Decimal("1.20")           # enter valuation TRIM band
    trim_release_price_to_base: Decimal = Decimal("1.10")   # leave TRIM band (hysteresis)
    exit_price_to_bull: Decimal | None = Decimal("1.00")    # price above bull value => EXIT proposal; None disables
    max_bear_downside: Decimal = Decimal("0.50")            # ADD requires bear value >= price * (1 - this)
    require_thesis_approval: bool = True
    require_valuation_approval: bool = True
    require_downside_review: bool = True
    max_valuation_age_days: int = 400
    max_price_age_sessions: int = 1
    max_filing_check_age_hours: int = 36
    max_financials_age_days: int = 200                      # latest period end older than this => overdue filing
    next_review_days: int = 90
    claim_value_tolerance: Decimal = Decimal("0.005")       # relative tolerance when matching a claimed number to evidence

    @model_validator(mode="after")
    def _bands(self):
        if self.add_exit_margin_of_safety > self.add_min_margin_of_safety:
            raise ValueError("add_exit_margin_of_safety must be <= add_min_margin_of_safety (hysteresis)")
        if self.trim_release_price_to_base > self.trim_price_to_base:
            raise ValueError("trim_release_price_to_base must be <= trim_price_to_base (hysteresis)")
        return self


class PortfolioPolicy(_Strict):
    target_holdings_min: int = 10
    target_holdings_max: int = 15
    max_issuer_weight: Decimal = Decimal("0.10")
    max_sector_weight: Decimal = Decimal("0.30")
    target_position_weight: Decimal = Decimal("0.08")
    min_trade_usd: Decimal = Decimal("50")
    fee_per_trade_usd: Decimal = Decimal("0")
    fractional_shares: bool = True
    cash_buffer_usd: Decimal = Decimal("0")
    allow_borrowing: Literal[False] = False
    residual_policy: Literal["hold_cash"] = "hold_cash"

    @model_validator(mode="after")
    def _limits(self):
        if self.target_position_weight > self.max_issuer_weight:
            raise ValueError("target_position_weight cannot exceed max_issuer_weight")
        return self


class ScreeningPolicy(_Strict):
    min_fiscal_years: int = 3
    min_completeness: Decimal = Decimal("0.70")
    min_peer_group_size: int = 5
    excluded_sic_ranges: list[tuple[int, int, str]] = Field(default_factory=lambda: [
        (6000, 6199, "BANK_OR_CREDIT"), (6300, 6411, "INSURANCE"), (6798, 6798, "REIT"),
        (6770, 6770, "SHELL"),
    ])
    biotech_sic: list[int] = Field(default_factory=lambda: [2834, 2835, 2836, 8731])
    biotech_min_revenue_usd: Decimal = Decimal("100000000")


class ConservativeGapPolicy(_Strict):
    """Version ``cg-1`` of the conservative growth variant (POLICY.md §13). Predeclared, NOT validated: it was fixed
    before any outcome was observed and must not be tuned to make a particular shortlist look plausible."""
    version: str = "cg-1"
    shrink_weight: Decimal = Decimal("0.5")          # growth used = w x own 3y CAGR + (1 - w) x peer median
    max_excess_over_peer_median: Decimal = Decimal("0.10")   # then capped at peer median + this
    min_peer_count: int = 20                         # peer median needs this many companies (SIC hierarchy fallback)


class SensitivityPolicy(_Strict):
    """One-at-a-time reverse-DCF changes; a candidate whose conservative gap is <= 0 under any of them is FRAGILE."""
    wacc_delta: Decimal = Decimal("0.01")
    terminal_growth_delta: Decimal = Decimal("0.005")
    margin_relative_delta: Decimal = Decimal("0.10")  # EBIT margins x (1 - delta)
    annual_dilution: Decimal = Decimal("0.01")        # share count grows this much per explicit year


class FinderEvaluationPolicy(_Strict):
    """Prospective research protocol (POLICY.md §13.7). Minimum observation requirements, not sufficient evidence."""
    primary_horizon_sessions: int = 126
    horizons_sessions: list[int] = Field(default_factory=lambda: [63, 126, 252])
    cost_bps_per_side: Decimal = Decimal("10")        # charged on each buy and each sell of a cohort member
    gate_min_months: int = 24
    gate_min_primary_cohorts: int = 52
    max_top5_issuer_share: Decimal = Decimal("0.5")   # share of positive excess contribution from the top 5 issuers
    bootstrap_samples: int = 2000
    ci_level: Decimal = Decimal("0.90")
    beta_lookback_sessions: int = 252


class LLMSelectionRule(_Strict):
    """Arm D: predeclared rule applied to the deterministic shortlist (arm B) using FROZEN judgments."""
    verdicts: list[str] = Field(default_factory=lambda: ["RESEARCH_FURTHER"])
    min_priority: int = 3


class FinderPolicy(_Strict):
    """Discovery of possibly under-rated companies. Produces RESEARCH candidates only — never ADD/TRIM/EXIT."""
    protocol_version: str = "finder-protocol-1"      # bump whenever any rule below changes
    min_market_cap_usd: Decimal = Decimal("300000000")
    # universe stage: last session volume x price — a SINGLE-SESSION PROXY (one unusual day can distort it)
    min_daily_dollar_volume_usd: Decimal = Decimal("1000000")
    trailing_liquidity_sessions: int = 20            # deep stage: median dollar volume over this many sessions
    peer_mapping: Literal["sic-v1"] = "sic-v1"       # SIC 4 -> 3 -> 2 digit -> division -> ALL, min size per stage
    exchanges: list[str] = Field(default_factory=lambda: ["NYSE", "Nasdaq"])
    # balance-sheet businesses the FCF/DCF screen cannot judge (Nasdaq industry labels; SIC exclusions apply again in the
    # deep dive). Excluding whole sectors would also drop e.g. ratings agencies, real-estate services or education firms.
    excluded_industries: list[str] = Field(default_factory=lambda: [
        "Major Banks", "Banks", "Commercial Banks", "Savings Institutions", "Property-Casualty Insurers", "Life Insurance",
        "Accident &Health Insurance", "Specialty Insurers", "Real Estate Investment Trusts",
        "Trusts Except Educational Religious and Charitable", "Finance Companies", "Finance/Investors Services",
        "Diversified Financial Services", "Investment Bankers/Brokers/Service", "Blank Checks"])
    excluded_sectors: list[str] = Field(default_factory=list)
    exclude_partnerships: bool = True             # LP/MLP "common units" issue K-1 tax forms; explicit choice
    countries: list[str] = Field(default_factory=lambda: ["United States"])  # US GAAP filers (10-K/10-Q)
    min_prelim_metrics: int = 3                 # bulk metrics required for a preliminary rank (missing != zero)
    sector_relative_min_size: int = 20          # rank within sector when it has at least this many members
    deep_dive_count: int = 60                   # top preliminary names fetched in full (filings, facts, prices)
    shortlist_size: int = 25
    min_years_history: int = 3
    expectations_gap_variable: Literal["revenue_growth"] = "revenue_growth"   # reverse-DCF variable solved for
    # dcf_margin_of_safety comes from the same DCF as the expectations gap, so it is shown but weighted 0 by default
    # (weighting both would count one model view twice)
    weights: dict[str, Decimal] = Field(default_factory=lambda: {
        "quality": Decimal("0.35"), "value": Decimal("0.30"), "expectations_gap": Decimal("0.35"),
        "dcf_margin_of_safety": Decimal("0")})
    max_judgments_per_run: int = 25
    report_top_priorities: int = 5                    # highlighted for research; all shortlisted names are kept
    conservative_gap: ConservativeGapPolicy = Field(default_factory=ConservativeGapPolicy)
    sensitivity: SensitivityPolicy = Field(default_factory=SensitivityPolicy)
    llm_selection_rule: LLMSelectionRule = Field(default_factory=LLMSelectionRule)
    evaluation: FinderEvaluationPolicy = Field(default_factory=FinderEvaluationPolicy)


class AlertPolicy(_Strict):
    cooldown_hours: int = 24
    price_move_alert: Decimal = Decimal("0.10")   # daily |move| that raises an INFO event (never a decision by itself)


class PaperPolicy(_Strict):
    slippage_bps: Decimal = Decimal("5")
    fee_per_trade_usd: Decimal = Decimal("0")
    fill: Literal["next_session_open"] = "next_session_open"


class ValuationDefaults(_Strict):
    explicit_years: int = 5
    terminal_growth_cap: Decimal = Decimal("0.04")
    default_wacc: Decimal = Decimal("0.09")
    default_terminal_growth: Decimal = Decimal("0.025")
    scenario_growth_shift: Decimal = Decimal("0.04")
    scenario_margin_shift: Decimal = Decimal("0.03")
    scenario_wacc_shift: Decimal = Decimal("0.01")


class MarketPolicy(_Strict):
    """Current-conditions rules. Market information can PAUSE purchases or raise research/review; it never
    creates ADD/TRIM/EXIT on its own and never relaxes portfolio limits. All thresholds provisional."""
    enabled: bool = True
    require_exposure_profile_for_purchase: bool = True
    pause_on_adverse_high_exposure: bool = True
    pause_on_unknown_high_exposure: bool = True          # missing data is UNKNOWN, not safe
    pause_on_market_stress: bool = False                 # opt-in: broad weakness alone is not a reason to wait
    unreviewed_event_pause_days: int = 30
    pause_reassess_days: int = 30
    cluster_window_before_days: int = 3
    cluster_window_after_days: int = 10
    stale_daily_days: int = 7
    stale_monthly_days: int = 75
    rates_1m_change_pp: Decimal = Decimal("0.50")
    hy_oas_level_pct: Decimal = Decimal("5.0")
    hy_oas_3m_change_pp: Decimal = Decimal("1.0")
    usd_3m_change: Decimal = Decimal("0.05")
    oil_3m_change: Decimal = Decimal("0.25")
    copper_3m_change: Decimal = Decimal("0.20")
    cpi_yoy_high: Decimal = Decimal("0.04")
    unemployment_3m_rise_pp: Decimal = Decimal("0.5")
    indpro_yoy_contraction: Decimal = Decimal("-0.02")
    market_drawdown: Decimal = Decimal("0.10")
    vix_elevated: Decimal = Decimal("30")
    short_interest_days_to_cover_research: Decimal = Decimal("8")
    short_interest_change_research: Decimal = Decimal("0.5")
    valuation_rate_change_proposal_pp: Decimal = Decimal("0.50")
    sector_relative_research: Decimal = Decimal("0.15")       # sector ETF 3m return vs SPY
    company_specific_move_research: Decimal = Decimal("0.15") # |company-specific 1m component|
    stress_wacc_shift: Decimal = Decimal("0.01")              # illustrative stress of the bear case (context only)
    evaluation_horizon_sessions: int = 63                     # fixed outcome horizon for pause episodes (descriptive)
    evaluation_min_episodes: int = 20                         # below: "insufficient evidence"; above: still descriptive


class Policy(_Strict):
    name: str = "provisional-preview"
    version: str = "0.1.0"
    status: Literal["PREVIEW", "APPROVED", "FROZEN"] = "PREVIEW"
    recommendation: RecommendationPolicy = RecommendationPolicy()
    portfolio: PortfolioPolicy = PortfolioPolicy()
    screening: ScreeningPolicy = ScreeningPolicy()
    alerts: AlertPolicy = AlertPolicy()
    paper: PaperPolicy = PaperPolicy()
    valuation: ValuationDefaults = ValuationDefaults()
    market: MarketPolicy = MarketPolicy()
    finder: FinderPolicy = FinderPolicy()

    def content_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


# ------------------------------------------------------------------ user settings
class ContributionSettings(_Strict):
    monthly_amount_usd: Decimal | None = None          # planned; not cash until recorded
    schg_relationship: Literal["supplement", "replace", "undecided"] = "undecided"


class NotificationSettings(_Strict):
    webhook_enabled: bool = False
    webhook_authorized: bool = False                    # explicit owner authorization
    webhook_url_env: str = "EQM_WEBHOOK_URL"            # secret stays in the environment
    ambiguous_timeout_policy: Literal["hold_for_review", "retry_same_key"] = "hold_for_review"
    max_attempts: int = 5


class LLMSettings(_Strict):
    # none | anthropic (API key, per-token billing) | claude_code (local `claude -p` under your logged-in Claude
    # account, no API key; counts toward your plan's usage limits) | fixture (tests)
    provider: Literal["none", "anthropic", "claude_code", "fixture"] = "none"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    use_server_fallbacks: bool = True
    monthly_budget_usd: Decimal | None = None          # required for paid providers (no unlimited spending)
    max_output_tokens: int = 16000
    price_input_per_mtok: Decimal | None = None        # explicit pricing for a model missing from the pricing table
    price_output_per_mtok: Decimal | None = None
    claude_code_bin: str = "claude"                    # path to the Claude Code CLI for provider claude_code
    claude_code_timeout_s: int = 900
    max_subscription_calls_per_day: int = 40           # claude_code: cap on calls/day (plan usage limits apply)
    # claude_code: `claude auth status` methods accepted as the owner's subscription login; anything else is refused
    claude_code_auth_methods: list[str] = Field(default_factory=lambda: ["claude.ai"])


class RiskSettings(_Strict):
    confirmed: bool = False            # owner has reviewed/approved the portfolio limits
    total_investable_assets_usd: Decimal | None = None
    outside_holdings_known: bool = False


class UserSettings(_Strict):
    display_timezone: str = "America/New_York"
    sec_user_agent: str | None = None  # SEC fair-access policy requires "Name email"
    market_data_provider: Literal["yahoo_chart", "csv", "fixture"] = "yahoo_chart"
    contribution: ContributionSettings = ContributionSettings()
    notifications: NotificationSettings = NotificationSettings()
    llm: LLMSettings = LLMSettings()
    risk: RiskSettings = RiskSettings()
    active_portfolio: str | None = None
    # contribution-matched comparisons; the FIRST is primary (S&P 500 via SPY for the side account)
    benchmarks: list[str] = Field(default_factory=lambda: ["SPY", "SCHG", "VTI"])
    # tax statuses whose portfolios get NO individual-company recommendations or allocations (e.g. a 401(k));
    # they can still be imported, reconciled and benchmarked
    no_company_research_tax_statuses: list[str] = Field(default_factory=lambda: ["TAX_DEFERRED"])
    finder_enabled: bool = False          # weekly company-finder scan (network: Nasdaq listing + SEC); opt-in
    # unattended judging in the weekly job; with claude_code it also needs a passing `eqm llm claude-check` for the
    # installed CLI version. Off by default: interactive packs are the initial default.
    finder_auto_judge: bool = False


def load_policy(path: str | Path | None) -> Policy:
    if path is None or not Path(path).exists():
        return Policy()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Policy.model_validate(data)


def load_user_settings(path: str | Path | None) -> UserSettings:
    if path is None or not Path(path).exists():
        return UserSettings()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return UserSettings.model_validate(data)
