# InvestorWB — systematic fundamental investing & portfolio monitor

A personal, long-only research and decision-support tool for US-listed operating companies. It keeps an auditable
ledger of your real holdings and preserves why you bought each company. It values businesses with transparent,
sourced DCFs, monitors SEC filings, and explains whether each holding is **ADD / HOLD / TRIM / EXIT / REVIEW**. It also
proposes where a monthly contribution could go. Each review combines the **broad market** (SPY, QQQ, rates, credit, inflation,
growth, employment, USD, commodities, VIX), the company's **sector/industry** benchmarks and **company-specific** information, and
reports a long-term action plus a separate **purchase eligibility** (ELIGIBLE / PAUSED / BLOCKED).

**It never trades.** Recommendations never modify holdings; you place orders yourself and record the fills.

**Research status.** Rules-based screening, DCF valuation and market-context conditions only. Price attribution is
descriptive, not a predictor; there is no predictive-model training and no walk-forward backtest pipeline. Paper and
performance tools measure a frozen policy prospectively and establish no edge.
All thresholds are provisional (see [POLICY.md](POLICY.md)) and outputs are labelled PREVIEW until you approve the
policy and supply your settings. Fixture and illustrative outputs are labelled as such and are not market evidence.

| document | contents |
|---|---|
| [docs/SPECIFICATION.md](docs/SPECIFICATION.md) | consolidated specification v1.1 (incl. market/sector/company context) |
| [POLICY.md](POLICY.md) | formulas, thresholds, rule precedence, limits, current conditions & purchase eligibility (§9) |
| [SOURCES.md](SOURCES.md) | every information source: availability, cost, licensing, delay, revisions, interpretation limits |
| [DATA_COVERAGE.md](DATA_COVERAGE.md) | sources, freshness, unsupported cases, historical limitations |
| [docs/CSV_SCHEMA.md](docs/CSV_SCHEMA.md) | transaction / snapshot / price CSV formats |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | running the scheduler, notifications, backup/restore, troubleshooting |
| [docs/DESIGN.md](docs/DESIGN.md) | repository audit, architecture, decisions, entities |
| [VALIDATION.md](VALIDATION.md) | what was actually run and tested |
| [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md) | what works, what is illustrative, what needs you |

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # core (no LLM)
uv sync --extra llm           # optional: Anthropic SDK for thesis drafting
uv run pytest                 # offline tests: no network, no credentials
uv run eqm init               # creates var/ database and config/user.yaml + config/policy.yaml from the examples
uv run eqm demo               # labelled FIXTURE demo -> reports/equity/demo/<date>/
uv run eqm --home var/demo dashboard     # http://127.0.0.1:8765 (FIXTURE demo)
```

### Credentials and settings (`config/user.yaml`, git-ignored)

| setting | needed for | without it |
|---|---|---|
| `sec_user_agent: "Your Name you@example.com"` | SEC filings/facts and FRED economic series (both refuse or stall requests without a contact email) | filings checks fail visibly (REVIEW); economic series MISSING → HIGH exposures PAUSED |
| `market_data_provider` | prices (`yahoo_chart` = unofficial; or `csv`) | — |
| `active_portfolio` | default portfolio for commands and jobs | pass `--portfolio` |
| `risk.confirmed`, `risk.outside_holdings_known`, `contribution.*` | personalization | output stays PREVIEW; household concentration UNKNOWN |
| `llm.provider: anthropic` + `ANTHROPIC_API_KEY` (or `ant auth login`) | LLM thesis drafts & filing summaries | no-LLM mode; everything else works |
| `notifications.webhook_enabled/authorized` + `EQM_WEBHOOK_URL` | external alerts | local inbox only |

## First real portfolio review (exact workflow)

```bash
# 0. one-time
uv sync && uv run eqm init
#    edit config/user.yaml: sec_user_agent, active_portfolio: main (keep risk.confirmed: false until you review limits)

# 1. portfolio and account
uv run eqm portfolio create main --kind ACTUAL
uv run eqm portfolio add-account brokerage --portfolio main --tax-status TAXABLE     # or TAX_DEFERRED / UNKNOWN

# 2. holdings: export your broker history or write opening positions (docs/CSV_SCHEMA.md; examples/)
#    Leave cost_basis blank when unknown; include external_id when your broker provides one.
uv run eqm import transactions brokerage my_transactions.csv       # re-running is safe (deduplicated)
uv run eqm import snapshot brokerage broker_positions.csv --as-of 2026-09-30   # discrepancies become issues

# 3. data
uv run eqm prices refresh                 # holdings + watchlist + benchmarks (SCHG, VTI)
uv run eqm sec sync MSFT --docs           # per company holding: link CIK, filings, XBRL facts, latest 10-K/10-Q text
uv run eqm reconcile --account brokerage  # provider splits/mergers vs your ledger
uv run eqm show                           # holdings, cash, weights, basis (unknown stays unknown)

# 4. research per company: valuation, then thesis (both need your approval)
uv run eqm valuation build MSFT --dump msft_valuation.yaml   # edit assumptions if needed, then:
uv run eqm valuation build MSFT --inputs msft_valuation.yaml --reason "my assumptions"
uv run eqm valuation show MSFT && uv run eqm valuation reverse MSFT --variable revenue_growth
uv run eqm valuation approve MSFT --downside-reviewed   # add --accept-assumptions only after reviewing the listed flags
uv run eqm thesis template > msft_thesis.yaml               # or: uv run eqm thesis draft MSFT (LLM, if configured)
uv run eqm thesis create MSFT --file msft_thesis.yaml --reason "original thesis"
uv run eqm thesis approve --version-id <printed id>    # FAILED claims block; SOURCE_MATCHED ones need --acknowledge-unverified
uv run eqm exposure draft MSFT --out msft_exposure.yaml     # benchmarks + sensitivities; evidence or ANALYST_ASSUMPTION each
uv run eqm exposure create MSFT --file msft_exposure.yaml --reason "initial exposure profile"
uv run eqm exposure approve --version-id <printed id>        # without an approved profile, purchases stay PAUSED

# 5. market context, review and allocate
uv run eqm market refresh                 # SPY/QQQ/sector ETFs, FRED series, FINRA short data -> shared snapshot
uv run eqm market show                    # the snapshot every company review references
uv run eqm review                         # recommendations + reports under var/reports/<date>/
uv run eqm allocate --hypothetical 1000   # labelled HYPOTHETICAL until you record the deposit
uv run eqm decide ACCEPT --recommendation <id> --rationale "..."   # your decision, recorded separately
uv run eqm dashboard

# 6. monitoring (keeps running only while this process runs; see docs/RUNBOOK.md)
uv run eqm serve
```

When you actually trade, record the fills (`eqm ledger add ...` or a CSV import) and run `eqm review` again.

## Command overview

`eqm portfolio|import|ledger|show|lots|reconcile|security|prices|sec|screen|valuation|thesis|exposure|market|watchlist|review|decide|allocate|research|evaluate|paper|jobs|serve|alerts|health|report|performance|demo|dashboard|backup|policy`
— run `uv run eqm <command> --help`.

## Layout

| path | purpose |
|---|---|
| `src/equity_monitor/config` | typed policy + user settings |
| `src/equity_monitor/data` | calendar, securities, SEC EDGAR, prices, raw store, HTTP |
| `src/equity_monitor/ledger` | append-only events, replay, CSV import, reconciliation, views |
| `src/equity_monitor/research` | PIT fundamentals, screening, theses, evidence verification, conditions |
| `src/equity_monitor/valuation` | DCF engine, assumption builder, versions |
| `src/equity_monitor/market` | source catalog & interpretation rules, PIT market series, price metrics, snapshots, exposure profiles, observation chains/clusters, portfolio look-through |
| `src/equity_monitor/decisions` | policy engine, current conditions (purchase eligibility), recommendations, allocation, owner decisions |
| `src/equity_monitor/llm` | provider-neutral interface, Anthropic adapter, prompts, validation |
| `src/equity_monitor/monitoring` | scheduler, jobs, events/alerts, outbox + webhook, health |
| `src/equity_monitor/evaluation` | returns math, benchmarks, performance, paper execution |
| `src/equity_monitor/reporting`, `dashboard` | reports/exports, local web UI |
| `src/equity_monitor/db/migrations` | SQL migrations |
| `reports/equity/` | committed sample reports (demo = FIXTURE, examples = ILLUSTRATIVE real data) |
| `scripts/e2e_real_company.py`, `scripts/e2e_market_context.py` | real-data end-to-end examples (ILLUSTRATIVE, separate data home) |
| `var/` | local data (git-ignored): database, raw provider responses, reports |
