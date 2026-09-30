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
  versioned theses with verified citations; LLM drafting behind a strict schema (no-LLM mode by default).
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
made that the augmented system improves outcomes; `eqm evaluate` reports "insufficient evidence" until ≥ 20 pauses have matured.
