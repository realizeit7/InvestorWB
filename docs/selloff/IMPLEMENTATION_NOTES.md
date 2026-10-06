# Post-Selloff Recovery Research — implementation notes

## Layout (research-only; never opens the portfolio database)

| path | role |
|---|---|
| `config/selloff_protocol.yaml`, `docs/selloff/PROTOCOL.md` | cohort protocol `sr-0.1` (hash stored with every run) |
| `src/equity_monitor/research/selloff/home.py` | isolated research home; refuses the portfolio home or any database holding portfolio data; research schema applied only here |
| `…/schema.sql` | append-only `sr_*` tables (UPDATE/DELETE triggers) |
| `…/discovery.py` | EDGAR full-text search, every query/page/total logged; fixed hash sampling order |
| `…/filings.py` | submissions index, 8-K + every EX-99 exhibit, cover-page ticker/exchange at the time, release dateline, report date |
| `…/timing.py` | earliest/latest public time, decline window, decision cutoff, first executable session |
| `…/screening.py` | recorded decisions with verbatim quotes; events built after the stop rule with deduplication |
| `…/records.py` | point-in-time sources (next report after the cutoff recorded as excluded), XBRL financing as filed by the cutoff, runway with stated formula, quote-verified candidate statements |
| `…/pricing.py` | prices with identity guard (ticker at the time, then the same CIK's current ticker), eligibility, outcome **coverage** audit (no returns) |
| `…/packs.py` | point-in-time evidence packs; judgment import labelled RETROSPECTIVE_CONTAMINATED, no probabilities, claims verified |
| `…/report.py`, `…/pipeline.py` | coverage table / funnel; idempotent, timed pipeline |
| `eqm study selloff init|discover|queue|record|status|run|pack|import-judgments|coverage` | research-only commands (`--research-home`, default `var/research/selloff`) |
| `scripts/selloff_feasibility.sh` | the exact reproducible run |
| `tests/test_selloff.py` | offline, credential-free tests (fake EDGAR and price source) |

## Records-quality rules added during implementation (not cohort rules)

- **Staleness:** a balance or annual value whose period ended more than 400 days before the cutoff is recorded as
  unknown (`STALE`), not as current. Found live: debt tags last reported in 2015 were returned as current debt for 2023
  events. This changes records only, never which events are in the cohort.
- **Prices through today:** event price histories are always requested through the current date (see shared finding 2).
- **Same-CIK fallback:** if the ticker at the time has no data, the issuer's current EDGAR tickers are tried with the
  same identity guard (name match with one of the CIK's EDGAR names, history starting before the event window).

The protocol file was not changed: its version string seeds the sampling order, so a version bump would reshuffle the
sample. These rules are documented here and in the code instead.

## Shared-component findings (reported, not repaired in this milestone)

| # | component | finding | effect here | proposed fix (separate change) |
|---|---|---|---|---|
| 1 | `research/fundamentals.debt_total` and `FactView.instant` | no staleness check: the latest date on which **any** debt concept was reported is used, even years before `as_of` | handled locally by the staleness rule | add a maximum age (policy key) and report stale aggregates as unknown; affects main-app valuation inputs for companies that changed debt tags |
| 2 | `data/prices.YahooChartProvider` + `parse_yahoo_chart` | the provider adjusts history for **every** later split, but only splits inside the requested range can be reversed; a historical window returns prices inflated by later reverse splits (e.g. 1:32 and 1:100 reverse splits years later) | found live (implausible market caps); fixed locally by requesting through today | document the requirement or always request through today when storing raw closes; the main app's rolling windows end today, so it is not affected today |
| 3 | `data/sec.html_to_text` | table cells are concatenated without a separator ("Common StockMRTXThe Nasdaq…") | cover parser handles both layouts | add a cell separator (changes stored passage text, so quotes and passage hashes would need a migration) |
| 4 | `research/fundamentals` concept map | no concepts for long-term investments, convertible notes, many collaboration-revenue tags | recorded as unknown with the reason | extend the map with tested precedence |
| 5 | EDGAR full-text search | intermittent HTTP 500 for valid queries | retried; every attempt logged | none needed |
| 6 | EDGAR `acceptanceDateTime` | checked: the `Z` suffix is correct UTC (matches SEC's index page) | none | none |

## Access limitations found

- ClinicalTrials.gov record history: `api/int/.../history` returns 403 and the history tab renders client-side.
- Free price sources: Yahoo drops delisted symbols; Stooq requires a browser challenge (not circumvented); Nasdaq's API
  returns "Symbol not exists" for delisted tickers.
- Tickers are reused: three event tickers now belong to unrelated listings.

## Reproducing

```bash
EQM_SEC_USER_AGENT="Your Name you@example.com" scripts/selloff_feasibility.sh var/research/selloff
# or step by step:
uv run eqm study selloff init --research-home var/research/selloff
uv run eqm study selloff discover --research-home var/research/selloff
uv run eqm study selloff queue --n 10 --research-home var/research/selloff        # screening view
uv run eqm study selloff record --file docs/selloff/screening_decisions.json --research-home var/research/selloff
uv run eqm study selloff run --research-home var/research/selloff
uv run eqm study selloff pack --research-home var/research/selloff
uv run eqm study selloff coverage --out coverage.csv --research-home var/research/selloff
uv run pytest tests/test_selloff.py
```
