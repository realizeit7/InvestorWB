# Market context — 2026-09-30

Snapshot `mks_e036b98ff3344fe48396` as of 2026-09-30T22:00:00.000000Z. Deterministic, point-in-time; referenced by company reviews.

## Broad market (reference ETFs and indices)

| Instrument | Status | 1m | 3m | 12m | From 52w high | Realized vol 3m |
|---|---|---|---|---|---|---|
| CL=F | OK | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| HG=F | OK | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| HYG | OK | -1.3% | -0.0% | 6.6% | -1.3% | 1.6% |
| QQQ | OK | 1.8% | 2.8% | 9.5% | -0.2% | 1.6% |
| SPY | OK | -0.6% | 0.5% | 8.1% | -1.1% | 1.6% |
| TLT | OK | -0.1% | 0.9% | 8.5% | -0.9% | 1.6% |
| ^VIX | OK | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| ^VIX3M | OK | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |

VIX / VIX3M ratio: 0.89 (implied volatility term structure; not a probability).

## Economy and financial markets

| Series | Status | Value | Period | Public at | Δ1m | Δ3m | y/y | Vintage |
|---|---|---|---|---|---|---|---|---|
| ICE BofA US Corporate (IG) OAS (`fred:BAMLC0A0CM`) | OK | 1 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| ICE BofA US High Yield OAS (`fred:BAMLH0A0HYM2`) | OK | 6.2 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 3.20 | 3.20 | 106.7% | FIRST_SEEN |
| CPI, all urban consumers (SA index) (`fred:CPIAUCSL`) | OK | 333.043 | 2026-09-01 | 2026-09-30T22:00:00.000000Z | 0.83 | 2.49 | 3.0% | FIRST_SEEN |
| WTI crude oil spot (`fred:DCOILWTICO`) | OK | 70 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| Effective federal funds rate (`fred:DFF`) | OK | 3.75 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| 10-year Treasury yield (`fred:DGS10`) | OK | 4 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| 2-year Treasury yield (`fred:DGS2`) | OK | 3.8 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| Nominal broad US dollar index (`fred:DTWEXBGS`) | OK | 120 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| Industrial production index (`fred:INDPRO`) | OK | 103 | 2026-09-01 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| Nonfarm payrolls (thousands) (`fred:PAYEMS`) | OK | 161600 | 2026-09-01 | 2026-09-30T22:00:00.000000Z | 100.00 | 300.00 | 0.7% | FIRST_SEEN |
| Retail sales (advance, $m) (`fred:RSAFS`) | OK | 716000 | 2026-09-01 | 2026-09-30T22:00:00.000000Z | 1,000.00 | 3,000.00 | 1.7% | FIRST_SEEN |
| 10y-2y Treasury spread (`fred:T10Y2Y`) | OK | 0.2 | 2026-09-29 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |
| Unemployment rate (`fred:UNRATE`) | OK | 4 | 2026-09-01 | 2026-09-30T22:00:00.000000Z | 0.00 | 0.00 | 0.0% | FIRST_SEEN |

## Sectors (SPDR sector ETFs)

| Sector | ETF | Status | 1m | 3m | vs SPY 3m |
|---|---|---|---|---|---|
| Communication Services | XLC | OK | -0.6% | 0.5% | 0.0% |
| Consumer Discretionary | XLY | OK | -1.3% | -0.0% | -0.5% |
| Consumer Staples | XLP | OK | -0.1% | 0.9% | 0.4% |
| Energy | XLE | OK | -0.3% | 1.2% | 0.8% |
| Financials | XLF | OK | 1.6% | 3.1% | 2.7% |
| Health Care | XLV | OK | 1.8% | 2.8% | 2.3% |
| Industrials | XLI | OK | -0.1% | 0.9% | 0.4% |
| Materials | XLB | OK | -0.1% | 0.9% | 0.4% |
| Real Estate | XLRE | OK | -0.6% | 0.5% | 0.0% |
| Technology | XLK | OK | -1.3% | -0.0% | -0.5% |
| Utilities | XLU | OK | 2.7% | 3.9% | 3.4% |

## Flags (conditions, not forecasts)

- **CREDIT_TIGHTENING** (CREDIT_CONDITIONS): HY OAS 6.2% (+3.20pp over 3m) — threshold >= 5.0% or +1.0pp/3m
- **CREDIT_TIGHTENING** (REFINANCING): HY OAS 6.2% (+3.20pp over 3m) — threshold >= 5.0% or +1.0pp/3m

## Missing, stale and unavailable

- Missing (UNKNOWN): none
- Stale: none
- Backfilled current-vintage history (may include later revisions): none
- Not available: single-stock options (IV/skew/term structure/OI); ETF fund flows; ETF holdings look-through; futures positioning (CFTC COT, deferred); licensed news feed
- Price co-movement is association, not causation.
- Implied volatility is a market price of options, not a probability of any business outcome.
