# Design (M0) — audit, architecture, decisions

## 1. Repository audit (2026-09-30)

- **`realizeit7/InvestorWB`** (this repo, the primary home per the owner's instruction): empty — no commits, no
  instructions. Everything in this project lives here, on branch `claude/equity-monitor-foundation`.
- **`realizeit7/Financial_exp`** (the spec's reference repo): cloned read-only for inspection, then the local clone was
  deleted. Nothing was written to it. Findings:
  - `main` holds only a README. The BTC research is on the unmerged branch `claude/quant-research-foundation-4xxpm5`
    (head `d9d3890`, PR #1): package `quant_research` (pandas/numpy/scikit-learn/pyarrow, `uv`, pytest), CLAUDE.md,
    STATUS.md, DECISIONS.md, `reports/SUMMARY.md`, `reports/AUDIT_2026-09-30.md`.
  - Its CLAUDE.md seals the final BTC interval **2025-09-01..2026-08-31** (config loading refuses overlap). This project
    reads no BTC data and imports nothing from that package, so the sealed holdout is untouched by construction.
  - Its STATUS recommends stopping the BTC track; M4 (stocks) was never built there and SEC hosts were blocked in that
    environment. Here, SEC EDGAR is reachable.
  - The previously proposed weekly stocks-plus-BTC ranking experiment was **not** started.
- Reuse decision: no code is imported (the BTC package is a backtesting stack; this is a ledger/research/monitoring app).
  Conventions reused as ideas: `uv` + lockfile, UTC tz-aware timestamps, "available_at ≤ decision_at", missing ≠ zero,
  immutable raw caches with SHA-256, labelled synthetic fixtures, decisions log.

**Integration boundary.** The two projects are independent. If they are ever combined, the only sanctioned interface is
read-only export files (this app's `decisions_export.json`, performance JSON); neither side may read the other's database,
and nothing may open or reuse the sealed BTC interval.

## 2. Architecture

```
config ─► policy + user settings (typed, versioned by content hash)
data   ─► providers (SEC EDGAR, Yahoo chart / CSV / fixture), calendar, securities, raw store (immutable)
ledger ─► append-only events ─► deterministic replay ─► positions, lots, cash, income, P&L ─► reconciliation
research ─► PIT fundamentals ─► screening ─► thesis versions + evidence verification ◄─ llm (drafts only)
valuation ─► FCFF DCF, scenarios, sensitivity, reverse DCF ─► immutable versions + approvals
decisions ─► pure policy engine ─► recommendations (immutable) ─► allocator ─► owner decisions (separate)
monitoring ─► scheduler ─► collect ─► detect events ─► alerts (inbox) ─► outbox ─► webhook (opt-in)
evaluation ─► contribution-matched benchmarks, TWR/MWR, drawdown, paper execution, process metrics
reporting/dashboard ─► Markdown/HTML reports, JSON exports, local web UI (read-mostly)
```

Analytical services never import the UI or a specific LLM provider. The LLM layer is provider-neutral
(`NoLLM`, `FixtureLLM`, `AnthropicProvider`) and its output can only become a draft or an unverified claim.

## 3. Key technical decisions

| # | decision | why |
|---|---|---|
| 1 | Python 3.11, `uv`, deps: pydantic, PyYAML, Jinja2, requests, tzdata; optional `anthropic` | small, typed, matches the owner's other project |
| 2 | SQLite (WAL, FK on) with ordered SQL migrations | single-user app; transactional; easy backup |
| 3 | Time series in SQLite, not Parquet | 10–50 securities × daily bars is tiny; Parquet can be added for a large universe |
| 4 | Decimal stored as TEXT; UTC ISO timestamps | exact money arithmetic; unambiguous ordering |
| 5 | Append-only ledger/theses/valuations/recommendations/decisions enforced by DB triggers | audit trail cannot be silently rewritten |
| 6 | Derived portfolio state recomputed from events (no stored balances) | one source of truth; reconciliation compares, never overwrites |
| 7 | FIFO lots; unknown basis/acquisition date stay unknown | informational only; no tax optimization without explicit assumptions |
| 8 | Point-in-time facts by `public_at`; restatements are new rows | no look-ahead; tested |
| 9 | EDGAR `acceptanceDateTime` treated as UTC (verified on live data) | exact public time for after-close filings |
| 10 | Rule-based NYSE calendar with explicit special closures | no dependency; testable holidays, early closes, DST |
| 11 | Recommendation engine is a pure function over a typed input record | deterministic, unit-testable precedence |
| 12 | stdlib WSGI dashboard + CSRF token, bound to 127.0.0.1 | no web framework needed for a personal tool |
| 13 | One external adapter: generic JSON webhook with idempotency key | works with most chat/notification relays; disabled by default |
| 14 | Yahoo chart as default price source, clearly labelled unofficial | free, works today; replaceable via `PriceProvider` |
| 15 | Anthropic adapter uses the official SDK, structured JSON output, adaptive thinking (effort `high`), server-side refusal fallback on by default (`llm.use_server_fallbacks`) | per Claude API guidance; strict local re-validation regardless |

## 4. Persistent entities (spec §17 → tables)

Security/issuer → `security`, `issuer`; identifier history → `security_identifier`; universe snapshot → `universe_snapshot`,
`universe_member`; account → `portfolio` (kind ACTUAL/PAPER/FIXTURE/HYPOTHETICAL), `account`; ledger event → `ledger_event`,
`import_batch`; tax lot / cash & position snapshot → derived by replay (`ledger/replay.py`), broker snapshots in
`brokerage_snapshot(_line)`; reconciliation issue → `reconciliation_issue`; source document / raw object →
`source_document`, `document_passage`, `raw_object`; normalized fact → `financial_fact`; data-quality issue →
`data_quality_issue`, `source_check`; thesis version → `thesis`, `thesis_version`, `thesis_approval`; claim & evidence link →
`claim`, `evidence_link`; milestone / invalidation condition → `milestone`, `invalidation_condition`, `condition_assessment`;
valuation version / scenarios & assumptions → `valuation_version` (inputs_json/outputs_json), `valuation_approval`;
policy version → `policy_version`; recommendation → `recommendation`; allocation proposal → `allocation_proposal`; user
decision → `user_decision`; job run → `job_run`; detected event → `detected_event`; alert → `alert`, `alert_status_log`;
delivery attempt → `delivery_outbox`, `delivery_attempt`; benchmark snapshot → `benchmark_snapshot`; paper execution →
`paper_execution`; evaluation result → `evaluation_result`; cost record → `cost_record`, `llm_call`; audit → `audit_log`.

## 5. Unresolved owner settings (never invented)

Current holdings and cash; account tax status; total investable assets; outside/retirement holdings; risk-limit approval;
whether the $1,000/month replaces or supplements SCHG; notification destination and authorization; SEC contact string;
LLM provider/credentials/budget. Until supplied, personalized outputs are labelled PREVIEW.
