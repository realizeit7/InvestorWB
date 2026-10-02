# Implementation summary

Built from an empty `realizeit7/InvestorWB` repository, as a standalone package (`src/equity_monitor`, CLI `eqm`) with
its own reports namespace (`reports/equity/`). The BTC repo `Financial_exp` was only read; its sealed 2025-09..2026-08
holdout was never touched and the stocks-plus-BTC ranking experiment was not started.

## What works

- **M1 Portfolio tracker**: append-only ledger (deposits, withdrawals, buys, sells, fees, dividends, DRIP, splits, opening
  balances, reversals/corrections); deduplicated CSV import; FIFO lots with unknown basis kept unknown; settled vs unsettled
  cash; long-only enforcement; brokerage snapshot reconciliation; provider split/merger checks; portfolio/sector/issuer
  weights; NAV; realized/unrealized gains; dividends; informational holding-period report; backup/restore.
- **M2 Research & valuation**: live SEC EDGAR ingestion (filing index, XBRL facts, filing text) with provenance and
  point-in-time `public_at`; normalization (tags, units, YTD→quarters, TTM, restatements); screening with documented
  metrics; FCFF DCF (bear/base/bull, sensitivity, reverse DCF, terminal-value share) with sourced assumptions;
  versioned theses with checked citations (citation integrity and substantive support are separate statuses since the
  repair milestone); LLM drafting behind a strict schema (no-LLM mode by default).
- **M3 Decisions**: deterministic engine (business assessment + action, spec precedence, hysteresis, reason codes,
  what-changed, change conditions); owner decisions/overrides recorded separately; monthly allocator with limits,
  rounding, fees, remaining cash.
- **M4 Monitoring**: idempotent scheduler in America/New_York, daily/weekly/monthly jobs, filing-event detection with
  8-K item severity, alerts with cooldown (CRITICAL never suppressed), local inbox, outbox + one webhook adapter
  (disabled by default), health report, dated Markdown/HTML reports, JSON exports, local dashboard.
- **M5 Evaluation**: contribution-matched SCHG/VTI benchmarks, TWR, MWR with root checks, drawdown, turnover, fees,
  research/LLM costs, NAV reconciliation, paper execution at next open (requires a FROZEN policy), process-quality metrics.

## Tested

99 automated tests (all 28 spec acceptance items mapped in [VALIDATION.md](VALIDATION.md)) plus live runs against SEC
EDGAR and Yahoo prices, a full CLI workflow, and browser rendering of the dashboard.

## Illustrative only

`reports/equity/demo/` (FIXTURE, fictional ZZ* companies), `reports/equity/examples/` (ILLUSTRATIVE real-data example,
nothing approved), all valuation defaults (9% WACC, 2.5% terminal growth, scenario shifts), all policy thresholds.

## Requires credentials

SEC contact User-Agent (free), optional Anthropic API credentials for LLM drafting, optional webhook URL.

## Requires your settings or data

Transactions/opening positions and a broker snapshot; account tax status; approval of `config/policy.yaml` limits
(`risk.confirmed`); total investable assets and outside holdings; monthly contribution and whether it replaces or
supplements SCHG; your own theses and valuation approvals per company; notification destination and authorization.

## Scheduling and notifications

**Scheduling is not running.** It needs `eqm serve` (or cron/systemd, see [docs/RUNBOOK.md](docs/RUNBOOK.md)) on a
machine that stays on. **External notification delivery is not enabled**: only the local inbox is active until you set
`webhook_enabled` + `webhook_authorized` and `EQM_WEBHOOK_URL`.

## Next steps (suggested, not started)

Point-in-time universe/delisting data (paid provider decision), more XBRL concepts (segments, leases), IR-release
ingestion, a value/quality benchmark, and a pre-registered prospective evaluation plan before any performance claim.

## v1.1 market-context amendment

Extended the existing architecture (no restart): new `market/` package (sources, series, metrics, snapshot, exposures, impacts,
lookthrough, external), `decisions/conditions.py`, migration `0002_market_context.sql`, eligibility-aware allocation with a
fundamental-only baseline, `evaluation/augmented.py`, paper variants, CLI (`eqm market|exposure|research|evaluate|paper`), reports
(10 items per holding + market context), dashboard Market view. Consolidated spec: [docs/SPECIFICATION.md](docs/SPECIFICATION.md);
sources: [SOURCES.md](SOURCES.md); rules: [POLICY.md §9](POLICY.md).

### Which inputs actively affect decisions

| input | can change | how |
|---|---|---|
| SEC filings/XBRL (company) | action and eligibility | v1.0 rules (REVIEW on new financials etc.); unreviewed MATERIAL/CRITICAL 8-K → PAUSED |
| Approved exposure profile | eligibility | defines which market/sector developments are relevant; missing profile → PAUSED |
| FRED rates (10y), HY/IG credit spreads, CPI, unemployment/payrolls, industrial production, USD, WTI; copper futures | eligibility; valuation *proposals* | snapshot flags adverse to a HIGH exposure → PAUSED; missing indicators for HIGH exposure → PAUSED (UNKNOWN); 10y move since valuation → WACC proposal (needs approval) |
| Prices (stock, SPY/QQQ, sector/industry ETFs, VIX) | eligibility only if you opt in to `pause_on_market_stress`; otherwise nothing | broad weakness/strength never changes actions |
| Portfolio limits, cash reconciliation | eligibility (BLOCKED) | unchanged v1.0 limits; market context cannot relax them |

### Context only (shown, clustered, may raise research tasks; never pauses or changes an action)

Sector/industry relative performance, price attribution (market/sector/company-specific, association only), realized volatility,
liquidity/volume, VIX level and term structure, bond ETFs, short interest (research task when elevated), short-sale volume,
owner-entered external research (fact only when verified), LLM competing explanations, bear-case stress values.

### Unavailable or deferred

Single-stock options IV/skew/term structure/volume/open interest (unavailable: no free reliable source); ETF fund flows and holdings
look-through (unavailable/deferred; ETF sector exposure shown as UNKNOWN); CFTC futures positioning (reachable, deferred); ALFRED
vintage history (needs a FRED API key); licensed news feed (none; manual entry only); Treasury CSV (documented alternative, unwired).

### Tested

99 automated tests (19 new for this amendment; see VALIDATION.md §4) and a live run on real FRED/FINRA/Yahoo/SEC data. No claim is
made that the augmented system improves outcomes; `eqm evaluate` is descriptive only (see the repair section below).

## Correctness repair of a2d0ef8 (2026-09-30)

Seven review findings reproduced and fixed at the mechanism level, each with regression tests that fail on a2d0ef8
(`tests/test_repairs.py`, 38 tests; suite 137 passed). Details and before/after numbers: [VALIDATION.md §5](VALIDATION.md);
rules: [POLICY.md](POLICY.md) §5, §7, §8, §9, §10, §11 and the change log.

| # | area | what changed |
|---|---|---|
| 1 | evidence | citation integrity (`SOURCE_MATCHED`) separated from substantive support; numeric statements checked on metric, value, scale, unit, sign, direction, period; free text never VERIFIED; thesis/exposure approval and engine gates; LLM opinion never upgrades; migration 0004 |
| 2 | allocation | aggregate issuer exposure across share classes, post-fee/rounding revalidation, aggregate displayed weights, both variants |
| 3 | allocation | every candidate re-reviewed at the cutoff under current policy/evidence; 7-day reuse removed; future cutoffs refused; exclusions explained |
| 4 | paper | allocation-only purchases with per-variant eligibility, cash/fees/rounding/limits, no borrowing, TRIM to target weight, originating FROZEN policy binding, atomic idempotent fills (migration 0005) |
| 5 | fundamentals | separate debt concepts combined per balance-sheet date without double counting; unknown components flagged; assumptions never FACT; `--accept-assumptions`; migration 0003 |
| 6 | evaluation | benchmark replayed from inception then sliced (+ labelled rebased mode); late/unapplied flows; no negative units |
| 7 | monitoring | alerts on eligibility transitions with pause reasons, evidence and reassessment; reversal not suppressed by cooldown |
| — | evaluation | pause episodes, fixed horizon, per-proposal withheld cash, descriptive verdict |

New policy keys: `market.evaluation_horizon_sessions`, `market.evaluation_min_episodes`, `recommendation.claim_value_tolerance`
(policy content hash changes: re-freeze before paper execution). New CLI flags: `eqm valuation approve --accept-assumptions`,
`eqm thesis approve --acknowledge-unverified`, `eqm exposure approve --acknowledge-unverified`.

Research status: rules-based screening, DCF and market context only. Regression attribution is descriptive, not a
predictor. There is no predictive-model training or walk-forward backtest pipeline. Paper and performance tools establish
no edge. Passing tests establish software behaviour, not profitability.

Remaining limitations: the claim parser covers a fixed English metric vocabulary (other phrasing stays SOURCE_MATCHED);
real issuers need `eqm sec sync` to populate the new debt concepts; paper TRIM/limit sizing uses the paper book valued at
the previous close with the traded security at the fill price; episode grouping by (start date, codes) is a heuristic for
shared causes; exposure-profile versions created before the repair keep their original verification snapshot.

## Follow-up repair of af00fc1 (2026-10-01)

Three remaining HIGH findings reproduced and fixed with 13 regression tests (suite 150 passed; details in
[VALIDATION.md §6](VALIDATION.md)):

| # | what changed |
|---|---|
| R1 | Claim numbers carry a role (current level, prior/comparison value, change); support needs same metric and role; a comparison value is a period's level only with its own stated period; self-contradictory from/to claims fail; unreadable roles stay SOURCE_MATCHED. Verifier `ev-3`; migration 0007 downgrades claims verified by `ev-2`. The certifiable claim format is documented in POLICY §10. |
| R2 | Paper execution state includes all fills through the execution session at open-time prices; one allocation per paper book per session (code + trigger); paper limits sized net of fees. |
| R3 | Paper sell identity is (recommendation, paper portfolio) — migration 0006 rebuilds `paper_execution` preserving rows — so augmented and baseline books both execute a TRIM/EXIT once. |

Remaining limitations: claims outside the documented wording (and figures the source does not state, such as derived
change amounts) are never VERIFIED and need a human; claims verified earlier must be re-created to be verified under
ev-3; a second proposal for an already-executed session must wait for the next session or use another paper book; the
paper-book valuation uses the previous close for untraded positions.

## Repair after the review of be46212 (2026-10-01)

| # | what changed |
|---|---|
| P1a | A direction asserted on any figure ("decreased to $4 billion") must be supported by the source's wording or a comparison it states (prior value, cited earlier-period fact); contradictions FAIL, missing comparisons stay SOURCE_MATCHED. Verifier `ev-4`. |
| P1b | Approvals cover the evidence state they were given: non-verified FACT claims need an explicit, append-only evidence review of their current status (`thesis_evidence_review`, migration 0008) before the thesis supports new ADDs or allocations. Re-approval re-verifies claims from older verifiers first; FAILED claims need a corrected version. Pending reviews block ADD only (HOLD, `EVIDENCE_REVIEW_REQUIRED`) — never a sale. Migration 0008 also downgrades ev-3 VERIFIED claims. |

Tests: 9 new (suite 159 passed). Details and before/after: [VALIDATION.md §7](VALIDATION.md). Remaining limitations: exposure
profiles keep their creation-time verification snapshot; re-verification runs on re-approval/review, not automatically;
the certifiable claim wording remains narrow (POLICY §10).

## Review of a1a330d and supervised-pilot readiness (2026-10-01)

| area | what changed |
|---|---|
| P1 exposure evidence | Verification snapshots record the verifier version; approval re-checks evidence under the current verifier; an approved profile checked by an older verifier is re-checked before it supports purchases (FAILED/unresolved → purchases paused, never a sale); append-only `exposure_evidence_check` (migration 0009). |
| P2 LLM spend | `llm/budget.py`: budget + known pricing required for paid calls; worst-case reservation before sending (exclusive transaction); settlement to actual usage; unknown cost blocks. Not a provider-enforced cap. |
| scope | TAX_DEFERRED (401(k)) portfolios: no company recommendations or allocations; side account only. |
| benchmark | SPY (S&P 500) is the primary contribution-matched benchmark; SCHG, VTI kept. |
| setup | `eqm setup check` (owner inputs, integrations; secrets as present/absent), `eqm --env-file`, `eqm alerts test`, RUNNING-row recovery fix. |
| pilot | `scripts/live_pilot.sh`, `scripts/pilot_ops_check.py`; checklist in [docs/PILOT_CHECKLIST.md](docs/PILOT_CHECKLIST.md). |

Tests: 175 passed. Live pilot, restart/recovery, local delivery mechanics and backup/restore were run (VALIDATION.md §8).
Owner-authorized notification, LLM live call, multi-day scheduling and real-holdings reconciliation are **not** run.

## Company finder and no-API LLM (2026-10-02)

The system now also **finds** candidates instead of only analysing companies the owner names:

| stage | what it does |
|---|---|
| universe | NYSE/Nasdaq US companies, market cap ≥ $300M, liquid, common shares, banks/insurers/REITs excluded (Nasdaq listing snapshot + SEC CIKs) |
| preliminary rank | SEC XBRL frames for all filers: growth, margins and trend, FCF margin/yield/consistency, sector-relative |
| deep dive (top 60) | full point-in-time filings/facts/prices; quality + value screen; reverse-DCF expectations gap (growth priced in vs delivered) |
| shortlist (25) | weighted under-rated score; append-only record |
| LLM judgment | under-rated case vs value-trap risks, verified claims, verdict + priority; cannot add names or create recommendations |
| evaluation | forward returns vs SPY at fixed horizons, descriptive |

LLM without an API key: `llm.provider: claude_code` (local `claude -p` under the owner's login; isolated, capped per
day) or the interactive pack (`eqm finder pack` → Claude Code session → `eqm finder import-judgments`). Commands:
`eqm finder run|show|judge|pack|import-judgments|promote|evaluate`; weekly job opt-in via `finder_enabled`.
Details: POLICY.md §13, RUNBOOK "LLM without an API key", VALIDATION.md §9.
Tests: 190 passed. A live stage-1 run (2,069 → 25), a pack export and one interactive judgment (TTD) were run; a live
`claude -p` call and prospective evaluation were not. Verifier ev-5: an inferred source period cannot contradict a claim.
