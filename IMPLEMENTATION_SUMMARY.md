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

80 automated tests (all 28 spec acceptance items mapped in [VALIDATION.md](VALIDATION.md)) plus live runs against SEC
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
