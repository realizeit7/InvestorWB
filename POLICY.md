# POLICY — formulas, thresholds, precedence and limits

**Every number in this document is a provisional engineering default, not a validated investment rule.**
The active values live in `config/policy.yaml` (template: `config/policy.example.yaml`). The policy is versioned by
content hash; every recommendation and allocation references the exact policy version it used.

Policy status:

| status | meaning |
|---|---|
| `PREVIEW` (default) | All personalized output is labelled PREVIEW. |
| `APPROVED` | You reviewed the values (`eqm policy approve` records your approval of that exact content hash). |
| `FROZEN` | Required before prospective paper execution, so paper results measure a policy fixed in advance. |

Output is also labelled PREVIEW while `risk.confirmed` is false in `config/user.yaml`, or when the portfolio is not ACTUAL.

## 1. Valuation (FCFF DCF) — `valuation/dcf.py`

```
FCFF_t      = EBIT_t × (1 − tax) + D&A_t − Capex_t − ΔNWC_t   (+ SBC_t only when sbc_treatment = add_back)
Revenue_t   = Revenue_{t−1} × (1 + g_t)          EBIT_t = Revenue_t × margin_t
ΔNWC_t      = nwc_pct × (Revenue_t − Revenue_{t−1})
TV_N        = FCFF_N × (1 + g_terminal) / (WACC − g_terminal)
EV          = Σ FCFF_t / (1+WACC)^t  +  TV_N / (1+WACC)^N          (end-of-year; optional mid-year)
Equity      = EV + cash & investments × excess_fraction + non-operating assets
              − debt − (lease liabilities if leases_as_debt) − minority interest − preferred − other claims
Value/share = Equity / diluted shares   (× (1+dilution)^N shares when SBC is added back)
Margin of safety = 1 − price / base value      (only if base value > 0 and meaningful)
```

Validation (hard errors): terminal growth < WACC; terminal growth ≤ `valuation.terminal_growth_cap` (4%); WACC > 0;
shares > 0; revenue > 0; tax in [0, 60%); margins in (−100%, 100%); SBC expensed ⇒ future dilution must be 0
(no double counting); SBC added back ⇒ dilution must be modelled. Debt is subtracted once, in the bridge
(FCFF is pre-financing). Warnings: WACC − g < 2pp; terminal value > 85% of EV; non-positive equity ("not meaningful").

Default assumption builder (`valuation/builder.py`), each value tagged `FACT | DERIVED | ANALYST_JUDGMENT | POLICY_DEFAULT | USER`:

| input | default |
|---|---|
| base revenue | TTM revenue (sum of 4 contiguous quarters) else latest fiscal year (FACT/DERIVED) |
| growth | 3-yr revenue CAGR clamped to [−5%, 15%], fading linearly to terminal growth over `explicit_years` (5) |
| EBIT margin | average operating margin of the last 3 fiscal years |
| tax | average effective rate clamped to [10%, 30%], else 21% (judgment) |
| D&A, capex | 3-yr average % of revenue; placeholders 3% / 4% if missing (judgment, flagged) |
| ΔNWC | 5% of incremental revenue (judgment placeholder) |
| WACC | `valuation.default_wacc` 9% (judgment — **not** a forecast return) |
| terminal growth | `valuation.default_terminal_growth` 2.5% |
| cash, debt, minority, leases | latest reported instants; missing cash/debt ⇒ `review_flags` shown before approval |
| shares | latest reported 3-month diluted weighted average, else latest FY, else shares outstanding |
| bear / bull | base ∓ `scenario_growth_shift` (4pp), ∓ `scenario_margin_shift` (3pp), ± `scenario_wacc_shift` (1pp) |

Missing revenue, margin history or share count raise `MissingInputs` ⇒ no valuation ⇒ REVIEW. Scenario probabilities,
when given, are labelled SUBJECTIVE. Reverse DCF solves one variable (constant growth, constant margin, WACC or terminal
growth) by bisection with all other inputs fixed, and reports `NOT_BOUNDED` / `NOT_IDENTIFIABLE` instead of guessing.

## 2. Business assessment

| state | rule |
|---|---|
| UNKNOWN | no approved thesis, unsupported business, or a METRIC invalidation condition cannot be evaluated (no data) |
| BROKEN | a pre-declared invalidation condition is TRIGGERED **and verified** (METRIC: evaluated by code from filed facts; EVENT/JUDGMENT: owner assessment) |
| WEAKENING | possible trigger not verified (LLM-suggested or AMBIGUOUS), latest period breaches a multi-period condition, or a milestone MISSED |
| INTACT | otherwise |

An LLM assessment can never verify an invalidation; it becomes AMBIGUOUS ⇒ REVIEW.

## 3. Portfolio action — rule precedence (`decisions/engine.py`)

1. **REVIEW** if any of: unsupported valuation framework (banks, insurers, REITs, shells, pre-revenue biotech, unclassified);
   open CRITICAL reconciliation issue on the holding; CRITICAL data-refresh failure; missing price or price older than
   `max_price_age_sessions` (1) completed sessions; filings not checked within `max_filing_check_age_hours` (36) or last
   check failed; latest financial period older than `max_financials_age_days` (200); no approved thesis; no valuation;
   valuation not approved; valuation older than `max_valuation_age_days` (400); financial statements published after the
   valuation's evidence cutoff; base value not meaningful; METRIC condition unevaluable; ambiguous/unverified invalidation;
   unreviewed verified CRITICAL event (e.g. 8-K items 1.03, 2.04, 3.01, 4.02, 5.01). Verified invalidations and limit
   breaches are still shown in `urgent`.
2. **EXIT** on a verified pre-declared invalidation.
3. **TRIM** to the limit on issuer (> `max_issuer_weight` 10%) or sector (> `max_sector_weight` 30%) breach.
4. **ADD** if business INTACT, margin of safety ≥ `add_min_margin_of_safety` (25%), downside reviewed, bear value ≥
   price × (1 − `max_bear_downside` 50%), issuer weight < `target_position_weight` (8%) and sector below its limit.
5. For held positions: **EXIT** if price > bull value × `exit_price_to_bull` (1.00); **TRIM** if price / base value >
   `trim_price_to_base` (1.20).
6. Otherwise **HOLD**, with the blocking reasons spelled out.

Hysteresis (anti-oscillation): once ADD, stay ADD-eligible until MoS < `add_exit_margin_of_safety` (20%); once TRIM,
stay until price/base < `trim_release_price_to_base` (1.10). Missing data never defaults to HOLD. A price move alone
never changes the business assessment and can only change the action through a comparison with an approved,
fact-based value.

Each recommendation stores: as-of, previous/current action, reason codes, changes since the previous review, thesis
and valuation version ids, policy version, supporting evidence with verification status, freshness, downside, concentration,
missing items, next review date, conditions that would change it. Identical inputs do not create a new row.

## 4. Concentration limits

Applied to the active portfolio **including cash**. Share classes of one issuer are combined. ETFs are excluded from
sector weights and from company valuation. Outside/retirement holdings are unknown unless supplied, so household-level
concentration is reported as UNKNOWN. Defaults: 10–15 holdings, 10% issuer, 30% sector, 8% target position, no borrowing.

## 5. Monthly allocation (`decisions/allocation.py`)

1. Candidates: holdings + APPROVED watchlist names whose latest recommendation (≤ 7 days old) is ADD.
2. Rank: margin of safety desc → latest screen score desc (missing last) → symbol asc (final tie-breaker).
3. Amount = min(target-weight room, issuer-limit room, sector-limit room, remaining budget − fee) against post-contribution NAV.
4. Skip amounts < `min_trade_usd` ($50); whole shares only if `fractional_shares: false`; fee `fee_per_trade_usd` ($0).
5. Remaining money stays cash; no candidate or binding limits ⇒ cash, explained. New names blocked beyond `target_holdings_max`.

Budget = settled available cash − `cash_buffer_usd` (+ hypothetical contribution, labelled HYPOTHETICAL). Unsettled sale
proceeds and proposed sales are excluded unless explicitly passed as conditional proceeds (labelled). Allocation is withheld
while NAV is unknown or cash history is unreconciled (negative cash).

## 6. Screening (`research/screening.py`)

Exclusions: SIC 6000–6199 banks/credit, 6300–6411 insurance, 6798 REIT, 6770 shells, pharma/biotech SIC with revenue <
$100M, unknown SIC, ETFs/funds, fewer than 3 fiscal years of revenue and operating cash flow. Metric definitions are in
the module docstring. Scores are peer-group percentiles (SIC 2-digit group with ≥ 5 members, else all eligible);
quality and value scores are averaged separately; a score is withheld when completeness < 70%. Invalid denominators give
`None`, never a favourable value. A screen score is a research priority, not a valuation or recommendation.

## 7. Evaluation

- Contribution-matched benchmark: same external flows on the same dates, executed at the close of the first session on
  or after the flow date, no fees, dividends reinvested at ex-date close, splits applied as units. Default benchmarks:
  SCHG and VTI (broad market). A value/quality comparison is not implemented yet.
- TWR: daily chain-linking with flows at the start of the day; MWR: XIRR reported only when there is exactly one root
  in (−99%, 10000%); drawdown on the TWR index; turnover = gross trades / average NAV. All pre-tax.
- Paper execution: fills at the open of the first session whose open is after the recommendation's creation time,
  `paper.slippage_bps` (5 bp) adverse, `paper.fee_per_trade_usd`; requires policy status FROZEN and a PAPER portfolio.

## 8. Monitoring

Default schedules (America/New_York, DST handled by zoneinfo): `daily_refresh` 18:30 on sessions; `weekly_digest`
Saturday 09:00; `monthly_allocation` 09:00 on the first session of each month. Alert cooldown 24 h for non-critical
alerts of the same kind and security; CRITICAL events are never suppressed. The first filings sync of an issuer is a
baseline: only filings public in the last 7 days raise events.
