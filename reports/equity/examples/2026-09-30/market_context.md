# Market context — 2026-09-29

Snapshot `mks_f870519a8c62432c95d3` as of 2026-09-30T15:50:31.731088Z. Deterministic, point-in-time; referenced by company reviews.

## Broad market (reference ETFs and indices)

| Instrument | Status | 1m | 3m | 12m | From 52w high | Realized vol 3m |
|---|---|---|---|---|---|---|
| CL=F | OK | 7.2% | 28.6% | 36.0% | -20.9% | 50.3% |
| HG=F | OK | -0.3% | 5.7% | 38.8% | -3.8% | 22.1% |
| HYG | OK | -2.5% | -1.8% | 1.2% | -2.7% | 3.3% |
| QQQ | OK | 3.1% | 0.3% | 24.4% | -1.3% | 19.6% |
| SPY | OK | -0.4% | 2.6% | 16.7% | -1.5% | 11.0% |
| TLT | OK | -5.2% | -8.4% | -8.0% | -11.5% | 9.9% |
| ^VIX | OK | 7.5% | -3.3% | -1.5% | -48.3% | 101.3% |
| ^VIX3M | OK | 3.5% | -4.8% | -1.7% | -38.2% | 48.7% |

VIX / VIX3M ratio: 0.89 (implied volatility term structure; not a probability).

## Economy and financial markets

| Series | Status | Value | Period | Public at | Δ1m | Δ3m | y/y | Vintage |
|---|---|---|---|---|---|---|---|---|
| ICE BofA US Corporate (IG) OAS (`fred:BAMLC0A0CM`) | OK | 0.84 | 2026-09-29 | 2026-09-30T15:49:30.071549Z | 0.05 | 0.08 | 12.0% | FIRST_SEEN |
| ICE BofA US High Yield OAS (`fred:BAMLH0A0HYM2`) | OK | 3.08 | 2026-09-29 | 2026-09-30T15:49:29.576824Z | 0.48 | 0.33 | 12.4% | FIRST_SEEN |
| CPI, all urban consumers (SA index) (`fred:CPIAUCSL`) | OK | 334.131 | 2026-08-01 | 2026-09-15T12:30:00.000000Z | 1.32 | 0.15 | 3.4% | FIRST_SEEN |
| WTI crude oil spot (`fred:DCOILWTICO`) | STALE | 96.41 | 2026-09-22 | 2026-09-23T22:00:00.000000Z | 9.20 | 21.79 | 53.1% | FIRST_SEEN |
| Effective federal funds rate (`fred:DFF`) | OK | 3.88 | 2026-09-28 | 2026-09-29T22:00:00.000000Z | 0.25 | 0.25 | -5.1% | FIRST_SEEN |
| 10-year Treasury yield (`fred:DGS10`) | OK | 5.24 | 2026-09-28 | 2026-09-29T22:00:00.000000Z | 0.51 | 0.86 | 24.8% | FIRST_SEEN |
| 2-year Treasury yield (`fred:DGS2`) | OK | 4.92 | 2026-09-28 | 2026-09-29T22:00:00.000000Z | 0.58 | 0.82 | 35.5% | FIRST_SEEN |
| Nominal broad US dollar index (`fred:DTWEXBGS`) | OK | 120.33 | 2026-09-25 | 2026-09-28T22:00:00.000000Z | 1.88 | -0.56 | -0.2% | FIRST_SEEN |
| Industrial production index (`fred:INDPRO`) | OK | 103.0682 | 2026-08-01 | 2026-09-18T13:15:00.000000Z | 0.02 | 0.43 | 1.4% | FIRST_SEEN |
| Nonfarm payrolls (thousands) (`fred:PAYEMS`) | OK | 159075 | 2026-08-01 | 2026-09-04T12:30:00.000000Z | 162.00 | 214.00 | 0.4% | FIRST_SEEN |
| Retail sales (advance, $m) (`fred:RSAFS`) | OK | 737763 | 2026-08-01 | 2026-09-18T13:15:00.000000Z | 8,225.00 | 6,348.00 | 5.4% | FIRST_SEEN |
| 10y-2y Treasury spread (`fred:T10Y2Y`) | OK | 0.37 | 2026-09-29 | 2026-09-30T15:49:28.380126Z | -0.02 | 0.07 | -28.8% | FIRST_SEEN |
| Unemployment rate (`fred:UNRATE`) | OK | 4.1 | 2026-08-01 | 2026-09-04T12:30:00.000000Z | 0.00 | -0.20 | -4.7% | FIRST_SEEN |

## Sectors (SPDR sector ETFs)

| Sector | ETF | Status | 1m | 3m | vs SPY 3m |
|---|---|---|---|---|---|
| Communication Services | XLC | OK | -1.0% | 4.4% | 1.8% |
| Consumer Discretionary | XLY | OK | -6.7% | -6.7% | -9.3% |
| Consumer Staples | XLP | OK | -3.6% | -0.8% | -3.4% |
| Energy | XLE | OK | -1.2% | 16.6% | 14.0% |
| Financials | XLF | OK | -6.7% | 1.1% | -1.5% |
| Health Care | XLV | OK | 0.1% | 8.0% | 5.4% |
| Industrials | XLI | OK | -4.3% | -8.4% | -11.0% |
| Materials | XLB | OK | -7.2% | -3.0% | -5.5% |
| Real Estate | XLRE | OK | -6.3% | -5.3% | -7.9% |
| Technology | XLK | OK | 4.9% | 2.2% | -0.4% |
| Utilities | XLU | OK | -6.4% | -11.8% | -14.4% |

## Flags (conditions, not forecasts)

- **RATES_UP** (RATES): 10y +0.51pp over ~1m — threshold |Δ| >= 0.50pp
- **RATES_UP** (REFINANCING): 10y +0.51pp over ~1m — threshold |Δ| >= 0.50pp
- **OIL_UP** (COMMODITY_OIL): WTI spot +29.2% over 3m — threshold |Δ| >= 25%

## Missing, stale and unavailable

- Missing (UNKNOWN): none
- Stale: fred:DCOILWTICO
- Backfilled current-vintage history (may include later revisions): none
- Not available: single-stock options (IV/skew/term structure/OI); ETF fund flows; ETF holdings look-through; futures positioning (CFTC COT, deferred); licensed news feed
- Price co-movement is association, not causation.
- Implied volatility is a market price of options, not a probability of any business outcome.
