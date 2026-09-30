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
    provider: Literal["none", "anthropic", "fixture"] = "none"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    use_server_fallbacks: bool = True
    monthly_budget_usd: Decimal | None = None
    max_output_tokens: int = 16000


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
    benchmarks: list[str] = Field(default_factory=lambda: ["SCHG", "VTI"])


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
