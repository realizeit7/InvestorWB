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
   check failed; latest financial period older than `max_financials_age_days` (200); no approved thesis; an approved
   thesis with a FACT claim that FAILED verification (`THESIS_EVIDENCE_FAILED`, §10); no valuation;
   (an approved thesis whose non-verified claims lack a review of their current status only blocks ADD —
   `EVIDENCE_REVIEW_REQUIRED`, §10 — it does not force REVIEW);
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

1. Candidates: holdings + APPROVED watchlist names. Each candidate is **re-reviewed at the allocation cutoff** under the
   current policy and current evidence (`generate(as_of=cutoff)`). An earlier recommendation is reused only when every
   decision input, the policy content hash, the exposure-profile version, the decision-relevant conditions and the
   eligibility hash identically ("equivalent earlier review reused" in the report); otherwise a new review row is written.
   Only a validated **ADD** qualifies. REVIEW (stale price, filings not checked, new financials since the valuation, …),
   HOLD/TRIM/EXIT and PAUSED/BLOCKED names are excluded with the reason codes. A cutoff in the future is refused; a
   recommendation dated after the cutoff is never used. (Replaces the former "latest recommendation ≤ 7 days old" rule.)
2. Rank: margin of safety desc → latest screen score desc (missing last) → symbol asc (final tie-breaker).
3. Amount = min(target-weight room, issuer-limit room, sector-limit room, remaining budget − fee) against post-contribution
   NAV. Target and issuer rooms use **aggregate issuer exposure**: every share class currently held plus everything already
   proposed for the same issuer in this run. The holdings cap (`target_holdings_max`) counts issuers.
4. Skip amounts < `min_trade_usd` ($50); whole shares only if `fractional_shares: false`; fee `fee_per_trade_usd` ($0).
5. **Revalidation**: after rounding and fees the complete proposal is re-checked against NAV after fees; an issuer or
   sector over its limit is cut back (last line first; whole shares if required), and a line that falls below the
   minimum trade is dropped. Displayed "proposed weight" is the aggregate issuer weight after the whole proposal.
6. Remaining money stays cash; no candidate or binding limits ⇒ cash, explained.

The same fill (same limits, same revalidation) runs for the augmented variant (`purchase_eligibility = ELIGIBLE`) and the
fundamental-only baseline (`baseline_eligibility = ELIGIBLE`); baseline lines carry their recommendation ids.

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
  or after the flow date (a flow on a weekend/holiday executes at the next session; if that session has no benchmark bar,
  at the next available bar with a "late execution" warning; with no bar through the end date it is listed as unapplied),
  no fees, dividends reinvested at ex-date close, splits applied as units. A withdrawal larger than the benchmark value
  empties it (warning); units never go negative. Default benchmarks: SCHG and VTI. A value/quality comparison is not
  implemented yet.
- **Subperiods**: the benchmark is replayed from inception (the first external flow) and then sliced to [start, end], so
  money contributed before the start date is invested in the benchmark too (mode `inception`, default). Mode `rebased`
  starts the benchmark at `start` with the portfolio NAV on the last session before `start` and is labelled REBASED.
- TWR: daily chain-linking with flows at the start of the day; MWR: XIRR reported only when there is exactly one root
  in (−99%, 10000%); drawdown on the TWR index; turnover = gross trades / average NAV. All pre-tax.
- **Paper execution** (`evaluation/paper.py`), PAPER portfolios only:
  - Bound to the **originating** decision's policy: the proposal's (or recommendation's) policy version must be FROZEN and
    must equal the active policy. Freezing after the decision is not enough.
  - Fills at the open of the first session whose open is after the decision's creation time, `paper.slippage_bps` (5 bp)
    adverse, `paper.fee_per_trade_usd`. If any needed bar is missing nothing is recorded (pending, all-or-nothing).
  - Purchases only through an allocation proposal, per variant: augmented lines need `purchase_eligibility = ELIGIBLE`,
    baseline lines `baseline_eligibility = ELIGIBLE`, both with action ADD under the proposal's policy/cutoff. Each line
    is capped by the proposal amount, the paper book's available cash (fees included), and issuer/sector limits measured
    on the paper book against NAV net of every fee the execution could charge; `fractional_shares` and `min_trade_usd`
    apply.
  - **Execution state** = every fill already recorded in the paper book through the execution session, including
    earlier fills of the same session, valued only with open-time prices (previous close, or the recorded fill price
    for a security already traded that session) — never the session's own close. Sells (TRIM/EXIT) use the same state.
  - **One allocation per paper book per session**: once a proposal/variant has executed for a session, any other
    proposal or variant filling on that session in that book is refused (enforced in code and by a database
    trigger); it neither adds to nor replaces the first. Use one paper book per variant. Paper execution **never borrows**: the ledger
    rejects any paper batch that would take cash below zero (`allow_negative_cash=False`). Broker imports keep negative
    cash as a reconciliation issue.
  - Direct recommendation execution: TRIM sells down to the recommendation's documented `proposed_trade.target_weight`
    (issuer weight in the paper book at the fill price, shares rounded up so the weight ends at or below the target;
    sector breaches without a target sell to the sector limit); EXIT sells the whole position. ADD is refused here.
  - Each (proposal, variant, paper portfolio) executes at most once, and each sell recommendation at most once **per
    paper portfolio** (`UNIQUE(recommendation_id, paper_portfolio_id)`, migration 0006), so the augmented and baseline
    books both execute it. Ledger rows and the execution record (`paper_allocation_execution` / `paper_execution`, with
    fills, skips and policy version) are written in one transaction.

## 8. Monitoring

Default schedules (America/New_York, DST handled by zoneinfo): `daily_refresh` 18:30 on sessions; `weekly_digest`
Saturday 09:00; `monthly_allocation` 09:00 on the first session of each month. Alert cooldown 24 h for non-critical
alerts of the same kind and security; CRITICAL events are never suppressed. The first filings sync of an issuer is a
baseline: only filings public in the last 7 days raise events.

The daily refresh (and the monthly allocation job, whose re-validation can record new reviews) compares each new
recommendation with its predecessor on **action and purchase eligibility**. Either
change creates one event (`ACTION_CHANGE` or `ELIGIBILITY_CHANGE`, keyed by recommendation id, so reruns never duplicate)
and a MATERIAL alert (CRITICAL for EXIT) that lists the pause codes, their evidence (observation ids and publication
times), the reassessment condition/date, and any blocks. For these alerts the cooldown is keyed on the exact transition
(e.g. "purchases ELIGIBLE → PAUSED"), so a reversal is never suppressed. Delivery outside the local inbox still requires
`webhook_enabled` + `webhook_authorized`.

## 9. Current conditions and purchase eligibility (market / sector / company context)

Every review produces two linked assessments:

| assessment | contents | outputs |
|---|---|---|
| **Long-term investment case** (§§2–3) | business quality, financial resilience, cash generation, valuation, thesis durability | business assessment + ADD / HOLD / TRIM / EXIT / REVIEW |
| **Current conditions** (`decisions/conditions.py`) | new developments, market context, event uncertainty, financing conditions | purchase eligibility **ELIGIBLE / PAUSED / BLOCKED** |

- **BLOCKED** — hard constraints: action REVIEW/TRIM/EXIT, business BROKEN, unsupported valuation, issuer or sector weight at/above
  its limit, unreconciled cash. Market context can never lift a block or relax a limit.
- **PAUSED** — no block, but wait. Each pause records a reason code, its source observations, a reassessment condition and a
  reassessment date (default +`market.pause_reassess_days` = 30):
  - `UNREVIEWED_MATERIAL_EVENT` — a MATERIAL/CRITICAL filing (e.g. 8-K 2.02 results, 5.02 officer change) published after your
    last approval/decision for the company and within `unreviewed_event_pause_days` (30).
  - `ADVERSE_<FACTOR>_HIGH_EXPOSURE` — a snapshot flag moves a factor against a **HIGH** approved exposure.
  - `UNKNOWN_CONDITION_HIGH_EXPOSURE` — every indicator linked to a HIGH exposure is missing (missing ≠ safe).
  - `NO_APPROVED_EXPOSURE_PROFILE` — sensitivities unknown (`require_exposure_profile_for_purchase`).
  - `MARKET_STRESS_POLICY` — only if you opt in (`pause_on_market_stress: true`, default **false**).
- **ELIGIBLE** — nothing blocks or pauses. The allocator buys only names with action **ADD and ELIGIBLE**. A HOLD can be ELIGIBLE
  (no condition prevents buying) yet receive no money, and a HOLD can be PAUSED.

### Observation → relevance → mechanism → implication → proposed decision

Relevance runs only through the company's **approved exposure profile** (versioned; each exposure has cited evidence or an
explicit ANALYST_ASSUMPTION label; direction is defined relative to the factor "rising", see SOURCES.md):

| situation | effect |
|---|---|
| development with no exposure path (e.g. oil shock for a software company) | CONTEXT_ONLY — cannot change eligibility or create a new recommendation row |
| broad-market moves (SPY/QQQ, VIX) | CONTEXT_ONLY (market risk is summarised once at portfolio level); pause only if opted in |
| favorable move on a linked exposure | NO_CHANGE (strength is not a reason to buy) |
| adverse move, MEDIUM/LOW exposure | NO_CHANGE with RISK implication (monitor) |
| adverse move, HIGH exposure | PAUSE_PURCHASES |
| MIXED/UNKNOWN direction or magnitude | RESEARCH_TASK (flag for review, no pause) |
| sector ETF ±`sector_relative_research` (15%) vs SPY over 3m | RESEARCH_TASK |
| company-specific price component ≥ 15% (statistical, not causal) | RESEARCH_TASK |
| short interest days-to-cover ≥ 8 or +50% | RESEARCH_TASK — never a sell signal |
| short-sale volume, VIX, futures prices/positioning | CONTEXT only |
| external claim | FACT only if verified against an ingested primary passage/fact; otherwise context |

Market information never creates ADD, TRIM or EXIT. Price co-movement is reported as a statistical attribution (market / sector /
company-specific components with prior-year betas), never as a cause.

### Clustering and double counting

- Observations are grouped into **clusters** (one development/hypothesis): market flags by family (RATES, CREDIT, FX, OIL, …,
  EQUITY_MARKET), sector moves by sector, and company observations around a filing anchor (window −3/+10 days). An earnings 8-K, the
  price drop, a short-interest rise, a short-volume spike and a headline in that window are **one** development with **one** effect.
- One flag can touch several factors (e.g. RATES_UP → RATES and REFINANCING) but yields one chain and at most one pause per cluster.
- Valuation changes from market data are **proposals** (e.g. WACC from the 10-year move since the valuation cutoff when
  |Δ| ≥ `valuation_rate_change_proposal_pp` 0.50pp), deduplicated per assumption, and must go through a new valuation version +
  approval. A bear-case stress (WACC +1pp) is shown for HIGH rate/refinancing exposures as context only.
- Broad market exposure is measured once for the whole portfolio (weight × beta to SPY/QQQ, cash = 0); company valuations carry
  no separate market-move penalty.

### Traceability

Each recommendation stores the snapshot id, exposure-profile version, thesis/valuation/policy versions, the chains with
observation ids, source ids and publication times, the fundamental-only **baseline eligibility**, and the change in eligibility
since the previous review. A new row is written only when a decision-relevant input changes.

### Evaluation (prospective, descriptive only)

`eqm evaluate` compares the augmented system with the fundamental-only baseline. Consecutive PAUSED-while-baseline-ELIGIBLE
ADD recommendations of one security are collapsed into one **pause episode** (daily rows are not independent observations);
episodes starting on the same day with the same pause codes are counted as one likely shared cause. Outcomes are measured
only for matured episodes over a **fixed horizon** (`market.evaluation_horizon_sessions`, 63 sessions) from the episode start (never "until today"), for the
stock, SPY and the sector ETF; missing data leaves an outcome unknown. Cash withheld versus the baseline is reported **per
proposal** (each proposal is a what-if on the same money) and the latest value; it is never summed. Also reported: source
success rates, alert usefulness ratings (`eqm alerts useful|not-useful`) and costs. Drawdown and benchmark-relative outcomes
of the two variants need two PAPER portfolios fed by `eqm paper --variant augmented|baseline` under a FROZEN policy.
Below `market.evaluation_min_episodes` (20) matured episodes the verdict is "insufficient evidence"; above it the output is
still **descriptive only** — no statistical test is run and 20 episodes are not a validation. No edge is claimed.

## 10. Evidence verification (`research/evidence.py`)

Two separate checks, stored separately on every claim (`citation_status`, `support_status`, `verifier_version`):

1. **Citation integrity** — the passage or fact exists, belongs to the same issuer, was public at or before the cutoff,
   and a passage quote appears verbatim. A broken citation ⇒ `FAILED`. An intact citation proves only that the quote is
   real: `SOURCE_MATCHED`.
2. **Substantive support** — the claim is parsed into quantitative statements (metric, value, scale, unit, sign,
   direction, period and **role**) and each is compared with the statements in the full source sentence(s) around the quote, or with
   the cited facts (concept, value within `recommendation.claim_value_tolerance` = 0.5%, fiscal year/quarter). A periodic filing's fiscal period is the default
   period of its unlabelled figures.

**Roles and the certifiable claim format** (verifier `ev-3`). A number's role is read from the words directly before it:

| role | wording | example |
|---|---|---|
| LEVEL — value for the stated period | was / were / is / of / at / to / reached / totaled X; "<metric>: X"; "<metric> X" | "Revenue was $4.2 billion in fiscal 2025" |
| PRIOR — starting or comparison value | from X; compared with/to X; versus / vs / against X | "…from $3 billion…" |
| CHANGE — amount or rate of change | increased/decreased/rose/fell… X; by X; up/down X; an increase/decline of X | "up 12%" |

A statement is supported only by a source statement with the same metric **and the same role** (plus value, unit, sign,
period, and direction for changes). A PRIOR value counts as the LEVEL of a period only when the source states that
period right after it ("from $3.75 billion in fiscal 2024"); otherwise its period is unknown and it is never treated as
the current result. A claim whose own from/to values contradict its direction ("increased from $4 billion to $3
billion") FAILS. **Every asserted direction must be supported, whatever the role** (verifier `ev-4`): "decreased to $4
billion" needs evidence that the metric fell — the source's own direction word, or a comparison it states (a from/prior
value, or a cited earlier-period figure such as last year's XBRL fact). An opposite direction FAILS; no comparison in the
cited evidence leaves the claim SOURCE_MATCHED. Direction-neutral levels ("was $4 billion") need no comparison. Numbers whose role cannot be read, and figures the source does not state (e.g. a change amount derived
by subtraction), are never VERIFIED. Only this narrow format can be certified; any other prose stays SOURCE_MATCHED.

| status | meaning | can drive decisions |
|---|---|---|
| `VERIFIED` | every quantitative statement confirmed on all fields, and no other free-text assertion | yes |
| `FAILED` | broken citation, or a statement contradicted (value/scale, sign, direction, period) or its number absent | no; blocks approval |
| `SOURCE_MATCHED` | citation intact; claim is free text, or a field (metric, period, direction) could not be confirmed | no; review required |
| `UNVERIFIED` | no citation | no |
| `NOT_REQUIRED` | ASSUMPTION / OPINION (labelled, never verified) | — |

Gates: thesis approval refuses FACT claims that FAILED and requires `--acknowledge-unverified` for SOURCE_MATCHED /
UNVERIFIED ones; the acknowledgement is stored as an **evidence review** (`thesis_evidence_review`, append-only) listing
each claim with the exact status it had. An approval supports new ADDs only while every non-VERIFIED FACT claim of the
version is covered by a review of its *current* status (point in time: reviews at or before the decision). When a claim
is downgraded after approval (migrations 0004/0007/0008) or otherwise changes status, the approval stays on record but
the engine withholds ADD (HOLD, reason `EVIDENCE_REVIEW_REQUIRED`); TRIM/EXIT rules are unaffected and nothing is sold
because a review is pending. Re-running `eqm thesis approve --version-id …` on an approved version never succeeds
silently: claims recorded by an older verifier are first re-verified under the current one (stored citations, the
version's evidence cutoff); claims that now verify need nothing more, FAILED ones require a corrected version, and the
rest require `--acknowledge-unverified`, which records a fresh review; the engine returns REVIEW (`THESIS_EVIDENCE_FAILED`) for an approved thesis
with FAILED FACT claims; exposure-profile approval refuses FAILED evidence and requires acknowledgement for unverified
evidence; external observations and news claims act only when VERIFIED. An LLM's opinion that a claim is supported is
recorded as a note and never raises a status (`with_llm_assessment`). Migration 0004 downgraded claims verified by the
former quote/number matcher to SOURCE_MATCHED (`support_status = LEGACY`); migration 0007 did the same for claims verified
by `ev-2`, which checked numbers independently of their roles, and migration 0008 for `ev-3`, which checked direction only
on change figures.

Limits: the parser uses a fixed metric vocabulary (revenue, operating/net income, cash flow, capex, debt concepts, cash,
margins, EPS, shares, equity, assets); other phrasing stays SOURCE_MATCHED. It does not understand causal or comparative
language; those parts always need a human.

## 11. Balance-sheet aggregates in valuations (`research/fundamentals.py::debt_total/cash_total`)

Debt is modelled as separate concepts: `LongTermDebtNoncurrent`, `LongTermDebt` (includes current maturities),
`LongTermDebtCurrent`, `DebtCurrent` (all current debt), `ShortTermBorrowings`, `CommercialPaper`. The total is built at one
balance-sheet date (the latest with any debt concept): long-term part = noncurrent, else LongTermDebt − current maturities,
else LongTermDebt; current part = DebtCurrent (minus current maturities when LongTermDebt already includes them), else
current maturities + short-term borrowings (commercial paper only when short-term borrowings are not reported). Components
from other dates are never combined (warning). Unreported components are listed as missing — never zero. A valuation input
with missing components or an unreported item is labelled `ANALYST_JUDGMENT` (never FACT) with a review flag;
`eqm valuation approve` then requires `--accept-assumptions`, and the accepted flags are recorded. Screening treats an
incomplete debt total as unknown (leverage metrics withheld).

## Change log

- **2026-09-30 correctness repair** (review of a2d0ef8): §5 allocation re-validation at the cutoff and aggregate issuer
  limits with post-fee revalidation; §7 benchmark replay from inception and paper-execution rules; §8 eligibility-change
  alerts; §9 episode-based descriptive evaluation; §10 evidence verification split; §11 debt aggregation and assumption
  acknowledgement. New policy keys: `market.evaluation_horizon_sessions` (63), `market.evaluation_min_episodes` (20, was the
  module constant MIN_PAUSES_FOR_READOUT; the former MIN_DAYS_AFTER_PAUSE = 60 days measured "until today" is replaced
  by the fixed horizon), `recommendation.claim_value_tolerance` (0.005, was a module constant). The
  former "recommendation ≤ 7 days old" allocation rule was removed (replaced by re-validation at the cutoff). No other
  threshold changed. Adding keys changes the policy content hash, so a FROZEN policy must be re-frozen.
- **2026-10-01 follow-up repair** (review of af00fc1): §10 quantity roles and the certifiable claim format (verifier
  ev-3; ev-2 VERIFIED claims downgraded); §7 paper execution state includes same-session fills at open-time prices, one
  allocation per paper book per session, paper limits net of fees, sell identity per paper portfolio. No threshold changed.
- **2026-10-01 review of be46212**: §10 direction asserted on any figure must be supported (verifier ev-4; ev-3 VERIFIED
  claims downgraded by migration 0008); approvals cover the evidence state they were given — downgraded or changed claims
  need a recorded evidence review (re-verified first) before the thesis supports new ADDs; never forces a sale. No
  threshold changed.
