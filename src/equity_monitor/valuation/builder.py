"""Build bear/base/bull DCF inputs from point-in-time facts.

Every derived number cites the facts it came from. Where history cannot inform an input, the
value is labelled ANALYST_JUDGMENT (or POLICY_DEFAULT) so the owner can see and change it.
Critical missing inputs raise ``MissingInputs``; they are never replaced by zero.
"""

from __future__ import annotations

from decimal import Decimal

from ..config.models import ValuationDefaults
from ..research.fundamentals import Aggregate, FactValue, FactView, cash_total, debt_total
from .dcf import A, ScenarioInputs, Series, Source

ZERO = Decimal(0)


class MissingInputs(ValueError):
    def __init__(self, missing: list[str]):
        super().__init__("missing critical valuation inputs: " + ", ".join(missing))
        self.missing = missing


def _src(kind: str, facts: list[FactValue], note: str) -> Source:
    refs = ",".join(sorted({f.accession or f.fact_id or "?" for f in facts}))
    return Source(kind=kind, ref=refs or None, note=note)


def _avg(xs: list[Decimal]) -> Decimal:
    return sum(xs, ZERO) / Decimal(len(xs))


def _clamp(x: Decimal, lo: str, hi: str) -> Decimal:
    return max(Decimal(lo), min(Decimal(hi), x))


def _paired(fv: FactView, num: str, den: str, n: int = 3) -> list[tuple[FactValue, FactValue]]:
    d = {f.end: f for f in fv.annual(den) if f.value}
    pairs = [(f, d[f.end]) for f in fv.annual(num) if f.end in d and f.value is not None]
    return pairs[-n:]


def build_scenarios(fv: FactView, defaults: ValuationDefaults) -> dict[str, ScenarioInputs]:
    missing = []
    rev_ttm = fv.ttm("revenue")
    annual_rev = fv.annual("revenue")
    base_rev_fact = rev_ttm or (annual_rev[-1] if annual_rev else None)
    if base_rev_fact is None or base_rev_fact.value is None or base_rev_fact.value <= 0:
        missing.append("revenue (TTM or latest fiscal year)")
    margins = _paired(fv, "operating_income", "revenue")
    if len(margins) < 2:
        missing.append("operating income history (>= 2 fiscal years)")
    shares = fv.quarters("shares_diluted_weighted")
    shares_fact = shares[-1] if shares else (fv.annual("shares_diluted_weighted")[-1:] or [None])[0]
    if shares_fact is None or not shares_fact.value:
        so = fv.instant("shares_outstanding")
        shares_fact = so if so and so.value else None
    if shares_fact is None:
        missing.append("diluted share count")
    if missing:
        raise MissingInputs(missing)

    n = defaults.explicit_years
    g_term = defaults.default_terminal_growth
    # growth: 3y revenue CAGR from fiscal years, clamped, fading linearly to terminal growth
    if len(annual_rev) >= 4 and annual_rev[-4].value and annual_rev[-4].value > 0:
        f0, f1 = annual_rev[-4], annual_rev[-1]
        cagr = Decimal(str((float(f1.value) / float(f0.value)) ** (1 / 3) - 1))
        g_src = _src("DERIVED", [f0, f1], "3-year revenue CAGR, clamped to [-5%, 15%], fading to terminal growth")
    elif len(annual_rev) >= 2 and annual_rev[-2].value:
        f0, f1 = annual_rev[-2], annual_rev[-1]
        cagr = f1.value / f0.value - 1
        g_src = _src("DERIVED", [f0, f1], "1-year revenue growth (short history), clamped, fading")
    else:
        cagr = g_term
        g_src = Source(kind="ANALYST_JUDGMENT", note="insufficient history: terminal growth used for all years")
    g0 = _clamp(cagr, "-0.05", "0.15")
    growth = [g0 + (g_term - g0) * Decimal(t) / Decimal(n) for t in range(n)]

    m = _avg([o.value / r.value for o, r in margins])
    m_src = _src("DERIVED", [x for p in margins for x in p], f"average operating margin, last {len(margins)} FYs")

    tax_pairs = _paired(fv, "income_tax", "pretax_income")
    if tax_pairs:
        tax = _clamp(_avg([t.value / p.value for t, p in tax_pairs if p.value > 0] or [Decimal("0.21")]), "0.10", "0.30")
        tax_a = A(value=tax, source=_src("DERIVED", [x for p in tax_pairs for x in p],
                                           "average effective tax rate, clamped to [10%, 30%]"))
    else:
        tax_a = A(value=Decimal("0.21"), source=Source(kind="ANALYST_JUDGMENT", note="US federal statutory rate; no history"))

    def pct(concept: str, default: str, note: str) -> A:
        pairs = _paired(fv, concept, "revenue")
        if pairs:
            return A(value=_avg([c.value / r.value for c, r in pairs]),
                     source=_src("DERIVED", [x for p in pairs for x in p], f"average {concept}/revenue, last {len(pairs)} FYs"))
        return A(value=Decimal(default), source=Source(kind="ANALYST_JUDGMENT", note=note))

    dna = pct("dna", "0.03", "no D&A history; placeholder 3% of revenue")
    capex = pct("capex", "0.04", "no capex history; placeholder 4% of revenue")
    nwc = A(value=Decimal("0.05"), source=Source(kind="ANALYST_JUDGMENT",
                                                 note="net working capital = 5% of incremental revenue (placeholder)"))

    flags: list[str] = []

    def aggregate(agg: Aggregate, label: str) -> A:
        """Bridge input from a same-date aggregate. Unknown parts are an explicit, flagged assumption (0 until
        the owner approves the valuation with the assumption acknowledged); never labelled FACT."""
        refs = ",".join(sorted({c[3] or c[2] or "?" for c in agg.components})) or None
        if agg.value is None:
            flags.append(f"{label}: nothing reported; ASSUMED 0 — confirm before approving")
            return A(value=ZERO, source=Source(kind="ANALYST_JUDGMENT", note=f"{label} not reported; assumed 0 (unapproved)"))
        note = f"{label} as of {agg.period_end}: {agg.method}"
        for w in agg.warnings:
            flags.append(f"{label}: {w}")
        if agg.missing:
            flags.append(f"{label}: components not reported for {agg.period_end} ({', '.join(agg.missing)}); "
                         f"ASSUMED 0 — confirm before approving")
            return A(value=agg.value, source=Source(kind="ANALYST_JUDGMENT", ref=refs,
                                                    note=note + f"; unreported components assumed 0: {', '.join(agg.missing)}"))
        return A(value=agg.value, source=Source(kind=agg.basis or "DERIVED", ref=refs, note=note))

    def inst(concept: str, label: str) -> A:
        f = fv.instant(concept)
        if f is None or f.value is None:
            flags.append(f"{label}: not reported; ASSUMED 0 — confirm before approving")
            return A(value=ZERO, source=Source(kind="ANALYST_JUDGMENT", note=f"{label} not reported; assumed 0 (unapproved)"))
        return A(value=f.value, source=_src("FACT", [f], f"{label} as of {f.end}"))

    cash_agg, debt_agg = cash_total(fv), debt_total(fv)
    cash_a = aggregate(cash_agg, "cash & short-term investments")
    debt_a = aggregate(debt_agg, "debt")
    if cash_agg.period_end and debt_agg.period_end and cash_agg.period_end != debt_agg.period_end:
        flags.append(f"cash ({cash_agg.period_end}) and debt ({debt_agg.period_end}) come from different balance-sheet dates")
    mi_a = inst("minority_interest", "minority interest")
    lease_f = fv.instant("operating_lease_liabilities")
    lease_a = A(value=lease_f.value, source=_src("FACT", [lease_f], f"operating leases as of {lease_f.end}")) \
        if lease_f is not None and lease_f.value is not None else \
        A(value=ZERO, source=Source(kind="ANALYST_JUDGMENT", note="operating lease cost left in EBIT (leases not treated as debt)"))
    base_rev = A(value=base_rev_fact.value, source=_src("FACT" if not base_rev_fact.derived else "DERIVED",
                                                        [base_rev_fact], base_rev_fact.note or "latest fiscal-year revenue"))
    shares_a = A(value=shares_fact.value, source=_src("FACT", [shares_fact], f"diluted shares for period ending {shares_fact.end}"))
    wacc = A(value=defaults.default_wacc, source=Source(kind="POLICY_DEFAULT", ref="policy:valuation.default_wacc",
                                                        note="discount rate is an analyst judgment, not a forecast return"))
    gt = A(value=g_term, source=Source(kind="POLICY_DEFAULT", ref="policy:valuation.default_terminal_growth"))

    base = ScenarioInputs(
        name="base", base_revenue=base_rev, revenue_growth=Series(values=growth, source=g_src),
        ebit_margin=Series(values=[m] * n, source=m_src), tax_rate=tax_a, dna_pct_revenue=dna, capex_pct_revenue=capex,
        nwc_pct_incremental_revenue=nwc, wacc=wacc, terminal_growth=gt, cash_and_investments=cash_a, debt=debt_a,
        minority_interest=mi_a, lease_liabilities=lease_a,
        diluted_shares=shares_a, review_flags=flags)

    def shifted(name: str, sign: int) -> ScenarioInputs:
        gs, ms, ws = defaults.scenario_growth_shift, defaults.scenario_margin_shift, defaults.scenario_wacc_shift
        note = f"{name}: base {'+' if sign > 0 else '-'} policy scenario shifts (analyst judgment)"
        return base.model_copy(update={
            "name": name,
            "revenue_growth": Series(values=[x + sign * gs for x in growth], source=Source(kind="ANALYST_JUDGMENT", note=note)),
            "ebit_margin": Series(values=[m + sign * ms] * n, source=Source(kind="ANALYST_JUDGMENT", note=note)),
            "wacc": A(value=defaults.default_wacc - sign * ws, source=Source(kind="ANALYST_JUDGMENT", note=note)),
        })

    return {"bear": shifted("bear", -1), "base": base, "bull": shifted("bull", +1)}
