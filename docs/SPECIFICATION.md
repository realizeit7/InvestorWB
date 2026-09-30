# Specification — Systematic Fundamental Investing & Portfolio Monitor (v1.1, consolidated)

v1.0 (2026-09-30) defined the ledger, research, valuation, recommendation, allocation, monitoring and evaluation system.
v1.1 (2026-09-30) adds multi-level market context. This document states the consolidated requirements once; where v1.1
overlapped v1.0 it extends the existing modules instead of adding new ones. Detailed rules: [POLICY.md](../POLICY.md);
sources: [SOURCES.md](../SOURCES.md), [DATA_COVERAGE.md](../DATA_COVERAGE.md).

## 1. Scope

- Actual portfolio: **long-only US-listed operating-company common stocks** (plus tracked ETFs such as SCHG). Trading is
  manual. Excluded from actual trading: futures, options, short selling, leverage, derivatives, crypto, automated execution.
- **Research may use** information across companies, industries, the broader economy, ETFs and related markets — including
  bond, credit, commodity, futures and options market data — as inputs to judgment, never as trading instruments.
- Unknown owner settings stay unknown; personalized output is PREVIEW until supplied/approved.

## 2. Three levels analysed together

| level | coverage | implementation |
|---|---|---|
| Broad market | SPY and QQQ (reference ETFs), VIX/VIX3M, rates (10y, 2y, curve, fed funds), credit (HY/IG OAS), inflation (CPI), growth (industrial production, retail sales), employment (unemployment, payrolls), USD, oil, copper, bond ETFs | `market/series.py`, `market/snapshot.py` |
| Sector / industry | SPDR sector ETF per company, industry ETF where a sensible SIC match exists, relative performance vs SPY | `market/exposures.py`, snapshot `sectors` |
| Company | filings, facts, valuation, disclosures/events (8-K items), thesis, price attribution, liquidity, short interest, short-sale volume, verified external claims | existing research/valuation modules + `market/impacts.py`, `market/external.py` |

Guiding question per review: *what changed in the market, in the industry, and at the company — and does any of it change the
decision?* Co-movement is never presented as causation.

## 3. Information coverage (modular; availability documented per source)

Company fundamentals, earnings, debt, cash flow, dilution, capital allocation (SEC); industry context (sector/industry ETFs,
owner-entered research); macro (FRED); bond/credit/commodity/futures prices; stock & ETF behaviour (returns, drawdown, volume,
liquidity); ETF flows/holdings (UNAVAILABLE); options IV/skew/OI (single-stock UNAVAILABLE; index IV via VIX); short interest &
short-sale volume (FINRA); external research/news (manual, verified against primary sources). Every source documents
availability, cost, licensing, coverage, publication delay, revisions, history and interpretation limits (SOURCES.md).

## 4. Exposure profiles

Versioned, approved per company: sensitivities to rates, refinancing, credit, consumer/enterprise spending, input costs,
employment, commodities, USD, geography, regulation, customers, suppliers — each with evidence or an ANALYST_ASSUMPTION label.
Every consequential observation follows **observation → relevance → mechanism → implication → proposed decision**; unclear
cases stay context or become research tasks.

## 5. Two linked assessments

Long-term investment case (ADD/HOLD/TRIM/EXIT/REVIEW, unchanged v1.0 precedence) and current conditions with **purchase
eligibility ELIGIBLE / PAUSED / BLOCKED** (each pause: reason, sources, reassessment condition/date). Market information may
update supported assumptions (as proposals through the approval workflow), expand downside scenarios (context), trigger research,
affect allocation eligibility, or change nothing. It never equates short interest with SELL, put activity with bearish
conviction, weakness with liquidation, or strength with buying.

## 6. Interpretation and double-counting safeguards

Data-class rules (`market/sources.py`) distinguish short-sale volume vs short interest, ETF volume vs flows, options activity vs
intent, futures open interest vs forecasts, futures prices vs expected spot, implied volatility vs probabilities, and facts vs
interpretations/scenarios/decisions. Publication timestamps and revisions are point-in-time. Missing data is UNKNOWN. Related
observations are clustered into one development. Market risk overlapping SPY/QQQ/sector/stock exposures is counted once at
portfolio level; no unexplained repeated valuation penalties.

## 7. Monitoring and reporting

A shared, immutable, timestamped market snapshot is referenced by every company review of a run. Per holding the report shows:
action + eligibility; original vs current thesis; market, sector and company developments; supporting/contradicting evidence;
valuation-assumption changes; implications for size and the next contribution; missing data and freshness; conditions that would
change the decision. v1.0 accounting, recommendation history, overrides, monthly allocation, notification controls and health
checks are preserved; market context never bypasses cash, issuer or sector limits.

## 8. Evaluation

Prospective comparison of the fundamental-only baseline with the augmented system (source quality, alert usefulness, pauses,
cash drag, turnover, costs, drawdown, benchmark-relative outcomes). No assumption that more information helps; no edge claimed
from a short record.

## 9. Standing constraints (from v1.0)

Preserve the BTC research and its sealed holdout (separate repository, never read). Keep trading manual. Do not purchase data or
activate external notifications without authorization. Deterministic code for arithmetic/constraints; the LLM only for
source-grounded interpretation, competing explanations and summaries — never invented data or policy changes.
