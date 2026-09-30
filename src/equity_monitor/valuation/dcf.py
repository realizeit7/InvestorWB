"""Unlevered free-cash-flow (FCFF) DCF with explicit, sourced assumptions.

    FCFF_t = EBIT_t * (1 - tax) + D&A_t - Capex_t - dNWC_t
    EV     = sum_t FCFF_t / (1+WACC)^t  +  TV_N / (1+WACC)^N,   TV_N = FCFF_N * (1+g) / (WACC - g)
    Equity = EV + cash & investments (excess fraction) + non-operating assets
             - debt - (leases if treated as debt) - minority interest - preferred - other claims
    Value/share = Equity / diluted shares

Consistency rules enforced by ``validate``:
- FCFF is discounted at WACC (an equity-cash-flow model would use cost of equity; not implemented).
- Terminal growth must be strictly below WACC and at or below the policy cap.
- Debt is subtracted once, in the bridge (FCFF is pre-financing: no interest in the cash flows).
- Stock compensation: with ``sbc_treatment='expense'`` (default) SBC stays inside EBIT as a cost and
  current diluted shares are used, so future dilution must be 0 (else double counting). With
  ``'add_back'`` SBC is added to FCFF and future dilution must be modelled via ``annual_net_dilution``.
- Timing: end-of-year discounting by default (``mid_year=True`` discounts flows at t-0.5).

Nothing here is a forecast return: the discount rate and the price/value gap are not expected returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENGINE_VERSION = "fcff-dcf-1"
ZERO = Decimal(0)
ONE = Decimal(1)
SourceKind = Literal["FACT", "DERIVED", "ANALYST_JUDGMENT", "POLICY_DEFAULT", "USER"]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: SourceKind
    ref: str | None = None      # fact id / accession / document passage / 'policy:<field>'
    note: str | None = None


class A(BaseModel):
    """An assumption: a value plus where it came from."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: Decimal
    source: Source


class Series(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    values: list[Decimal]
    source: Source


def judg(value, note: str) -> A:
    return A(value=Decimal(str(value)), source=Source(kind="ANALYST_JUDGMENT", note=note))


class ValuationError(ValueError):
    pass


class ScenarioInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["bear", "base", "bull", "custom"] = "base"
    base_revenue: A
    revenue_growth: Series
    ebit_margin: Series
    tax_rate: A
    dna_pct_revenue: A
    capex_pct_revenue: A
    nwc_pct_incremental_revenue: A
    wacc: A
    terminal_growth: A
    cash_and_investments: A
    excess_cash_fraction: A = Field(default_factory=lambda: judg(1, "all cash treated as excess (default)"))
    nonoperating_assets: A = Field(default_factory=lambda: judg(0, "none identified"))
    debt: A
    lease_liabilities: A = Field(default_factory=lambda: judg(0, "operating lease cost left in EBIT"))
    leases_as_debt: bool = False
    minority_interest: A = Field(default_factory=lambda: judg(0, "none identified"))
    preferred_equity: A = Field(default_factory=lambda: judg(0, "none identified"))
    other_claims: A = Field(default_factory=lambda: judg(0, "none identified"))
    diluted_shares: A
    sbc_treatment: Literal["expense", "add_back"] = "expense"
    sbc_pct_revenue: A = Field(default_factory=lambda: judg(0, "not added back"))
    annual_net_dilution: A = Field(default_factory=lambda: judg(0, "SBC expensed; no extra dilution"))
    mid_year: bool = False
    probability: A | None = None   # SUBJECTIVE unless independently calibrated
    review_flags: list[str] = Field(default_factory=list)   # inputs the owner must check before approval

    @field_validator("revenue_growth", "ebit_margin")
    @classmethod
    def _nonempty(cls, v: Series):
        if not v.values:
            raise ValueError("series must not be empty")
        return v

    @model_validator(mode="after")
    def _lengths(self):
        if len(self.revenue_growth.values) != len(self.ebit_margin.values):
            raise ValueError("revenue_growth and ebit_margin must have the same number of years")
        return self

    @property
    def years(self) -> int:
        return len(self.revenue_growth.values)


def validate(s: ScenarioInputs, terminal_growth_cap: Decimal | None = None) -> list[str]:
    """Raise ValuationError on invalid inputs; return non-fatal warnings."""
    errs, warns = [], []
    w, g = s.wacc.value, s.terminal_growth.value
    if w <= 0:
        errs.append("WACC must be positive")
    if g >= w:
        errs.append(f"terminal growth {g} must be below WACC {w}")
    if terminal_growth_cap is not None and g > terminal_growth_cap:
        errs.append(f"terminal growth {g} exceeds policy cap {terminal_growth_cap}")
    if s.diluted_shares.value <= 0:
        errs.append("diluted shares must be positive")
    if s.base_revenue.value <= 0:
        errs.append("base revenue must be positive for this model")
    if not (ZERO <= s.tax_rate.value < Decimal("0.6")):
        errs.append("tax rate must be in [0, 0.6)")
    for m in s.ebit_margin.values:
        if not (Decimal("-1") < m < ONE):
            errs.append(f"EBIT margin {m} out of range (-100%, 100%)")
    if s.sbc_treatment == "expense" and s.annual_net_dilution.value != 0:
        errs.append("SBC is expensed in EBIT; also applying future dilution would double count")
    if s.sbc_treatment == "add_back" and s.annual_net_dilution.value <= 0:
        errs.append("SBC added back: future dilution must be modelled (annual_net_dilution > 0)")
    if not (ZERO <= s.excess_cash_fraction.value <= ONE):
        errs.append("excess_cash_fraction must be within [0, 1]")
    if errs:
        raise ValuationError("; ".join(errs))
    if w - g < Decimal("0.02"):
        warns.append("WACC - g spread below 2pp: terminal value is highly sensitive")
    return warns


@dataclass
class YearRow:
    year: int
    revenue: Decimal
    ebit: Decimal
    nopat: Decimal
    dna: Decimal
    capex: Decimal
    delta_nwc: Decimal
    sbc_addback: Decimal
    fcff: Decimal
    discount_factor: Decimal
    pv_fcff: Decimal


@dataclass
class DCFResult:
    scenario: str
    rows: list[YearRow]
    pv_explicit: Decimal
    terminal_value: Decimal
    pv_terminal: Decimal
    enterprise_value: Decimal
    bridge: dict[str, Decimal]
    equity_value: Decimal
    shares: Decimal
    value_per_share: Decimal
    terminal_share: Decimal
    meaningful: bool
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario, "pv_explicit": self.pv_explicit, "terminal_value": self.terminal_value,
            "pv_terminal": self.pv_terminal, "enterprise_value": self.enterprise_value, "bridge": self.bridge,
            "equity_value": self.equity_value, "shares": self.shares, "value_per_share": self.value_per_share,
            "terminal_share": self.terminal_share, "meaningful": self.meaningful, "warnings": self.warnings,
            "rows": [r.__dict__ for r in self.rows],
        }


def run_dcf(s: ScenarioInputs, terminal_growth_cap: Decimal | None = None) -> DCFResult:
    warns = validate(s, terminal_growth_cap)
    w, g = s.wacc.value, s.terminal_growth.value
    rev_prev = s.base_revenue.value
    rows: list[YearRow] = []
    for t in range(1, s.years + 1):
        rev = rev_prev * (ONE + s.revenue_growth.values[t - 1])
        ebit = rev * s.ebit_margin.values[t - 1]
        nopat = ebit * (ONE - s.tax_rate.value)
        dna = rev * s.dna_pct_revenue.value
        capex = rev * s.capex_pct_revenue.value
        dnwc = (rev - rev_prev) * s.nwc_pct_incremental_revenue.value
        sbc = rev * s.sbc_pct_revenue.value if s.sbc_treatment == "add_back" else ZERO
        fcff = nopat + dna - capex - dnwc + sbc
        exp = Decimal(t) - (Decimal("0.5") if s.mid_year else ZERO)
        df = ONE / (ONE + w) ** int(exp) if exp == int(exp) else ONE / _pow(ONE + w, exp)
        rows.append(YearRow(t, rev, ebit, nopat, dna, capex, dnwc, sbc, fcff, df, fcff * df))
        rev_prev = rev
    pv_explicit = sum((r.pv_fcff for r in rows), ZERO)
    tv = rows[-1].fcff * (ONE + g) / (w - g)
    df_n = ONE / (ONE + w) ** s.years      # terminal value is as of end of year N
    pv_tv = tv * df_n
    ev = pv_explicit + pv_tv
    bridge = {
        "enterprise_value": ev,
        "+cash_and_investments": s.cash_and_investments.value * s.excess_cash_fraction.value,
        "+nonoperating_assets": s.nonoperating_assets.value,
        "-debt": -s.debt.value,
        "-lease_liabilities": -s.lease_liabilities.value if s.leases_as_debt else ZERO,
        "-minority_interest": -s.minority_interest.value,
        "-preferred_equity": -s.preferred_equity.value,
        "-other_claims": -s.other_claims.value,
    }
    equity = sum(bridge.values(), ZERO)
    shares = s.diluted_shares.value
    if s.sbc_treatment == "add_back":
        shares = shares * (ONE + s.annual_net_dilution.value) ** s.years
    vps = equity / shares
    meaningful = equity > 0 and ev > 0
    if not meaningful:
        warns.append("equity or enterprise value not positive: per-share value is not meaningful")
    tshare = pv_tv / ev if ev != 0 else ZERO
    if tshare > Decimal("0.85"):
        warns.append(f"terminal value is {tshare:.0%} of EV")
    return DCFResult(s.name, rows, pv_explicit, tv, pv_tv, ev, bridge, equity, shares, vps, tshare, meaningful, warns)


def _pow(base: Decimal, exp: Decimal) -> Decimal:
    return Decimal(str(float(base) ** float(exp)))


# ------------------------------------------------------------------ sensitivity
def sensitivity(s: ScenarioInputs, wacc_steps=(-0.01, -0.005, 0, 0.005, 0.01),
                g_steps=(-0.01, -0.005, 0, 0.005, 0.01), cap: Decimal | None = None) -> dict:
    grid = []
    for dw in wacc_steps:
        row = []
        for dg in g_steps:
            w = s.wacc.value + Decimal(str(dw))
            g = s.terminal_growth.value + Decimal(str(dg))
            try:
                v = run_dcf(s.model_copy(update={"wacc": A(value=w, source=s.wacc.source),
                                                 "terminal_growth": A(value=g, source=s.terminal_growth.source)}),
                            cap).value_per_share
            except ValuationError:
                v = None
            row.append(v)
        grid.append(row)
    return {"wacc": [s.wacc.value + Decimal(str(x)) for x in wacc_steps],
            "terminal_growth": [s.terminal_growth.value + Decimal(str(x)) for x in g_steps], "value_per_share": grid}


# ------------------------------------------------------------------ reverse DCF
REVERSE_VARIABLES = ("revenue_growth", "ebit_margin", "wacc", "terminal_growth")


@dataclass
class ReverseResult:
    variable: str
    status: Literal["SOLVED", "NOT_BOUNDED", "NOT_IDENTIFIABLE"]
    implied_value: Decimal | None
    bounds: tuple[Decimal, Decimal]
    value_at_bounds: tuple[Decimal | None, Decimal | None]
    target_price: Decimal
    fixed_assumptions: dict
    note: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _with(s: ScenarioInputs, variable: str, x: Decimal) -> ScenarioInputs:
    src = Source(kind="DERIVED", note=f"reverse DCF trial value for {variable}")
    if variable == "revenue_growth":
        return s.model_copy(update={"revenue_growth": Series(values=[x] * s.years, source=src)})
    if variable == "ebit_margin":
        return s.model_copy(update={"ebit_margin": Series(values=[x] * s.years, source=src)})
    if variable == "wacc":
        return s.model_copy(update={"wacc": A(value=x, source=src)})
    if variable == "terminal_growth":
        return s.model_copy(update={"terminal_growth": A(value=x, source=src)})
    raise ValueError(variable)


def reverse_dcf(s: ScenarioInputs, price: Decimal, variable: str, bounds: tuple[Decimal, Decimal] | None = None,
                cap: Decimal | None = None, tol: Decimal = Decimal("0.00001")) -> ReverseResult:
    """Solve for the one ``variable`` that makes value/share equal ``price``; all else fixed.

    ``revenue_growth``/``ebit_margin`` are solved as a single constant applied to every explicit year.
    """
    if variable not in REVERSE_VARIABLES:
        raise ValueError(f"variable must be one of {REVERSE_VARIABLES}")
    if bounds is None:
        bounds = {
            "revenue_growth": (Decimal("-0.30"), Decimal("0.60")),
            "ebit_margin": (Decimal("-0.20"), Decimal("0.80")),
            "wacc": (s.terminal_growth.value + Decimal("0.005"), Decimal("0.30")),
            "terminal_growth": (Decimal("-0.05"), min(s.wacc.value - Decimal("0.005"), cap or Decimal("1"))),
        }[variable]
    fixed = {k: v for k, v in s.model_dump(mode="json").items() if k != variable}
    lo, hi = bounds

    def f(x: Decimal) -> Decimal | None:
        try:
            return run_dcf(_with(s, variable, x), cap).value_per_share - price
        except ValuationError:
            return None

    flo, fhi = f(lo), f(hi)
    vals = (None if flo is None else flo + price, None if fhi is None else fhi + price)
    if flo is None or fhi is None:
        return ReverseResult(variable, "NOT_BOUNDED", None, bounds, vals, price, fixed,
                             "model invalid at a search bound")
    if abs(fhi - flo) < Decimal("0.0001"):
        return ReverseResult(variable, "NOT_IDENTIFIABLE", None, bounds, vals, price, fixed,
                             f"value/share does not respond to {variable} within bounds")
    if (flo > 0) == (fhi > 0):
        return ReverseResult(variable, "NOT_BOUNDED", None, bounds, vals, price, fixed,
                             f"price {price} is outside the value range {vals[0]:.2f}..{vals[1]:.2f} "
                             f"attainable by varying {variable} within bounds")
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if fm is None:
            break
        if abs(fm) < tol or (hi - lo) < Decimal("1e-9"):
            return ReverseResult(variable, "SOLVED", mid, bounds, vals, price, fixed,
                                 "solved by bisection; all other assumptions held fixed")
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    mid = (lo + hi) / 2
    return ReverseResult(variable, "SOLVED", mid, bounds, vals, price, fixed, "bisection reached tolerance")


# ------------------------------------------------------------------ scenario helpers
def margin_of_safety(price: Decimal, base_value: Decimal | None, meaningful: bool = True) -> Decimal | None:
    """1 - price / base value; only when the base value is positive and meaningful."""
    if base_value is None or not meaningful or base_value <= 0:
        return None
    return ONE - price / base_value


def scenario_return(price: Decimal, future_value: Decimal, years: Decimal,
                    distributions: list[tuple[Decimal, Decimal]] | None = None) -> Decimal | None:
    """Annualized IRR of: pay ``price`` now, receive ``distributions`` [(t_years, amount)], sell at
    ``future_value`` at ``years``. An illustrative scenario calculation, NOT a forecast."""
    flows = [(ZERO, -price)] + list(distributions or []) + [(years, future_value)]
    from ..evaluation.returns import irr_from_times
    r = irr_from_times([(float(t), float(a)) for t, a in flows])
    return None if r.rate is None else Decimal(str(r.rate))


Builder = Callable[..., ScenarioInputs]
