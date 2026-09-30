# DATA COVERAGE — sources, freshness, unsupported cases, historical limitations

## Sources

| data | provider | access | status in this build |
|---|---|---|---|
| Filing index (10-K, 10-Q, 8-K, amendments, 20-F/40-F/6-K) | SEC EDGAR `data.sec.gov/submissions` | free; declared User-Agent with contact email required; ≤ 10 req/s (we use ~6/s) | **verified live 2026-09-30** (AAPL, MSFT, KO, PEP, CAT, JPM) |
| Financial facts (XBRL) | SEC `api/xbrl/companyfacts` | same | **verified live**; 3–4.4k facts per company normalized |
| Filing text | EDGAR Archives primary document | same | **verified live** (Apple 10-K: 165 passages) |
| Ticker ↔ CIK map | `sec.gov/files/company_tickers_exchange.json` | same | verified live; current tickers only (no history) |
| Daily prices, splits, dividends | Yahoo Finance chart endpoint (`yahoo_chart`) | **unofficial, undocumented, no license grant**; personal research only; may break or rate-limit (HTTP 429 seen on `query1`) | verified live; raw closes recovered from split-adjusted data (checked on NVDA 10:1, 2024-06-10) |
| Daily prices (alternative) | your CSV files (`csv` provider) | you control licensing | tested with fixtures |
| Company IR releases, news | — | not implemented | news may only *discover* events; primary evidence must come from filings |
| Fixture data | `equity_monitor/fixtures.py` | synthetic | labelled FIXTURE everywhere |

A SEC request without an email-shaped contact in the User-Agent is refused with HTTP 403. The Yahoo adapter can be replaced
by a licensed provider by implementing `PriceProvider.fetch`.

Market, economic and positioning sources (FRED, FINRA short interest / short-sale volume, reference ETFs, VIX, futures, and the
unavailable options/ETF-flow data) are documented in **[SOURCES.md](SOURCES.md)** with availability, cost, licensing,
coverage, publication delay, revisions, history, interpretation limits and their role (decision vs context).

## Three timestamps per document

1. **Fiscal period end** (`fiscal_period_end`, fact `period_end`).
2. **Public availability** (`public_at`): the EDGAR `acceptanceDateTime` (UTC; verified against Apple's 16:30 ET earnings
   8-K) when available; otherwise **23:59:59 ET on the filing date** (`FILED_DATE_END_OF_DAY`), so a fact is never usable
   before the next session.
3. **Retrieval** (`retrieved_at`) by this application.

Decisions use (2) only. Restatements are stored as new as-filed rows with their own `public_at`; earlier as-of queries
cannot see them (tested). Raw provider responses are stored immutably, content-addressed by SHA-256, with the parser and
normalizer versions recorded.

## Normalization rules

- Canonical concepts map to ordered XBRL tag lists (see `research/fundamentals.py`); per period the highest-priority tag
  wins, then the latest-known filing. The tag used is recorded.
- Durations are kept only for 3/6/9/12-month periods. Discrete quarters are derived from year-to-date values
  (Q2 = 6M − Q1, Q3 = 9M − 6M, Q4 = FY − 9M) **only for additive flows**; weighted-average share counts are never
  derived by subtraction. TTM = four contiguous quarters, else unknown.
- Units: USD for money, `shares` for share counts. **Non-USD reporters are unsupported** (their USD facts are simply
  missing → REVIEW).
- Missing facts stay missing (`None`), never zero. A valuation input that had to be assumed (e.g. cash or debt not
  found) carries a `review_flags` entry shown before approval, is labelled `ANALYST_JUDGMENT` (never FACT), and
  approval requires `--accept-assumptions`.
- **Debt** (normalizer `norm-2`): totals and components are separate concepts — `long_term_debt_noncurrent`
  (LongTermDebtNoncurrent), `long_term_debt_total` (LongTermDebt, which includes current maturities),
  `long_term_debt_current` (LongTermDebtCurrent), `debt_current` (DebtCurrent, all current debt), `short_term_borrowings`,
  `commercial_paper`. `debt_total()` combines them at one balance-sheet date without double counting (POLICY §11) and
  lists unreported components as missing; components dated differently are not combined. Cash = cash and equivalents +
  short-term investments at the same date. Migration 0003 remapped rows stored by `norm-1` (which kept only the first tag
  of a folded list) by their source tag; components `norm-1` never stored stay unknown until the issuer is re-synced
  (`eqm sec sync SYMBOL`). Scope: consolidated US-GAAP tags only; finance leases, IFRS tags and segment debt are not read.
- Known gaps: some filers stop using a tag (e.g. Apple's `InterestExpense` after FY2023) → those metrics become unknown;
  segment data, leases detail, off-balance-sheet items and non-GAAP measures are not extracted.

## Evidence (claims) — what "verified" means

A cited claim is `SOURCE_MATCHED` when its citation is intact (same issuer, public before the cutoff, verbatim quote);
it is `VERIFIED` only when every quantitative statement in it matches the cited sentence(s) or facts on metric, value,
scale, unit, sign, direction and period and it contains no other free-text assertion (POLICY §10). Contradictions and
numbers absent from the evidence are `FAILED`. The parser covers a fixed vocabulary of financial metrics in English; any
other phrasing, causal language or qualitative statements remain `SOURCE_MATCHED` (review required). Claims recorded
before this rule (verifier `legacy-1`) were downgraded from VERIFIED to SOURCE_MATCHED; exposure-profile versions are
immutable and keep the verification snapshot recorded at their creation.

## Freshness and quality gates

For each holding the system shows the latest price date and its age in completed sessions, when filings were last checked
(and whether that check failed), the latest financial period end, whether new financial statements appeared after the
valuation, and open data/reconciliation issues. The actionable price is the close of the **latest completed NYSE
session** (holidays and 13:00 early closes handled; data assumed available 2 h after the close). A failed refresh is
recorded as a CRITICAL data issue and a HEALTH alert, the job ends PARTIAL, and affected recommendations show REVIEW.
It is never reported as "nothing changed".

Monitoring is **scheduled polling** (after each session, weekly, monthly), not real-time.

## Unsupported cases (explicit)

| case | handling |
|---|---|
| Banks, insurers, REITs, shells, pre-revenue biotech | excluded from discovery; if held: accounting + filing monitoring only, valuation UNSUPPORTED ⇒ REVIEW |
| ETFs (e.g. SCHG) | tracked, priced, benchmarked; no company valuation or recommendation |
| Mergers, spinoffs, delistings, symbol changes | not auto-applied; the position is frozen and a CRITICAL reconciliation issue asks you to record the outcome |
| Provider split without a matching ledger SPLIT | MISSING_SPLIT_EVENT reconciliation issue |
| Non-USD currencies, options, shorts, margin, crypto | rejected / out of scope |
| Unscheduled market closures | must be added to `SPECIAL_CLOSURES` in `data/calendar.py` |

## Historical limitations

- Current screening uses today's ticker map. There is **no point-in-time universe membership, no delisted securities and
  no survivorship-free price history** (these need a paid provider; not purchased). Any historical screen or backtest built
  on this data must be labelled `ENGINEERING_ONLY`; `run_screen` records the label.
- EDGAR companyfacts only covers XBRL-era filings (roughly 2009+), and the calendar rules cover 2000–2030.
- LLM components cannot be evaluated cleanly on history: models may know later outcomes from training. Evaluate them
  prospectively only.
- No investment-performance claim is supported by the fixtures or by a short live record.
