# SOURCES — information coverage, availability and interpretation limits

Generated from `src/equity_monitor/market/sources.py` (`uv run eqm market sources`). Roles: **DECISION** = can pause purchases,
raise REVIEW or feed valuation proposals through the documented rules; **CONTEXT** = shown and clustered, may raise research
tasks, never pauses or changes an action by itself; **DEFERRED** = reachable but not integrated yet; **UNAVAILABLE** = no
reliable free source. Nothing was purchased. Missing information is shown as UNKNOWN, never as neutral.

| source | role | access / cost / licensing | coverage | publication delay | revisions | history | interpretation limits |
|---|---|---|---|---|---|---|---|
| SEC EDGAR filings + XBRL facts (`sec_edgar`) | DECISION | public HTTPS API; declared User-Agent with contact email; <=10 req/s; free; public domain (US government) | US SEC registrants; XBRL facts ~2009+ | acceptance timestamp (seconds) | amendments are new filings; kept as new rows | full EDGAR history | as-filed data; non-GAAP, segments and guidance text are not normalized |
| Yahoo Finance chart endpoint (stocks, ETFs, indices such as ^VIX, futures such as CL=F) (`yahoo_chart`) | DECISION | unofficial, undocumented HTTPS endpoint; rate limited; free; no license grant; personal research only | US stocks/ETFs; CBOE indices; front-month continuous futures | end of day (we wait 2h after close) | split-adjusted closes are un-adjusted by us; occasional provider corrections are not versioned | decades for large ETFs; continuous futures roll without adjustment | volume is trading activity, not investor flows; continuous futures series have roll jumps |
| SEC EDGAR full-text search (`sec_fts`) | RESEARCH (selloff study only) | public HTTPS endpoint (efts.sec.gov); declared User-Agent; free | 8-K text incl. exhibits, 2001+ | near real time | n/a | per query | exact-phrase search; misses other wording, press-release-only and 6-K announcements; shows TODAY's names/tickers; intermittent HTTP 500 |
| ClinicalTrials.gov API v2 | not used (checked) | public; current record only — history endpoint 403 here | registered trials | n/a | history not retrievable here | — | current status must never stand in for the historical record |
| FRED graph CSV (St. Louis Fed) (`fred`) | DECISION | public CSV download, no key; free; public; some series (ICE BofA spreads) carry third-party terms and limited history | US macro and market series | daily market series ~1 business day; CPI/payrolls ~2-6 weeks after period | CURRENT VINTAGE ONLY without an API key: backfilled history may include later revisions (flagged); revisions seen after first retrieval are stored as new rows | decades (series-dependent) | release dates are estimated from publication-lag rules unless provided; ALFRED vintages need a FRED API key (not configured) -> deferred |
| FINRA consolidated short interest (API) (`finra_short_interest`) | CONTEXT | public API (api.finra.org), no key for this dataset; free; FINRA terms of use | exchange-listed and OTC equities | twice monthly; published ~7-8 business days after settlement (estimated) | revision flag provided by FINRA; revised values stored as new rows | 2020+ via API | a stock of open short positions at settlement date; says nothing about hedging motives; never a sell signal |
| FINRA Reg SHO daily short-sale volume files (`finra_regsho_daily`) | CONTEXT | public flat files (cdn.finra.org), no key; free; FINRA terms of use | trades reported to FINRA facilities only (not total consolidated volume) | next business day | rare; not versioned | rolling archive | short-SALE VOLUME includes market-maker hedging and is NOT outstanding short interest |
| US Treasury daily par yield curve CSV (`treasury_rates`) | CONTEXT | public CSV; free; public domain | nominal par yields 1m-30y | same day after ~15:30 ET | rare corrections | 1990+ | alternative to FRED DGS series; not wired by default |
| CFTC Commitments of Traders (`cftc_cot`) | DEFERRED | public text files; free; public domain | futures positioning by trader category (weekly, Tuesday data) | published Friday 15:30 ET for Tuesday data | occasional | 1986+ | open interest/positioning is not a directional forecast; mapping contracts to companies is weak; reachable; deferred until an exposure clearly needs it |
| Single-stock options (IV, skew, term structure, volume, open interest) (`options_chains`) | UNAVAILABLE | no free reliable source (Yahoo options endpoint returns 401); paid vendors (e.g. OPRA-derived); vendor license | - | - | - | - | activity is not investor intent or unhedged direction; IV is not an objective probability; market-level implied volatility is available via ^VIX / ^VIX3M indices |
| ETF fund flows, full holdings, breadth (`etf_flows_holdings`) | UNAVAILABLE | issuer holdings files exist (e.g. SSGA xlsx) but formats change; flows need paid data; flows: paid; issuer/vendor terms | - | holdings daily; flows daily (vendor) | - | - | trading volume is not a fund flow; look-through sector weights of held ETFs stay UNKNOWN; deferred: holdings files could be added for SPY/QQQ/SCHG |
| Nasdaq.com stock screener download (all NYSE/Nasdaq/AMEX listings) (`nasdaq_screener`) | CONTEXT | unofficial JSON endpoint api.nasdaq.com/api/screener/stocks (no key); browser-like User-Agent needed; free; no license grant; personal research only | US exchange listings: symbol, last price, market cap, volume, country, sector, industry | end of day / delayed snapshot | not versioned (snapshot overwritten) | current snapshot only (no history, so no point-in-time universe) | used ONLY to choose which companies the finder screens (size, liquidity, country, sector); never a valuation input — valuations use SEC shares x stored prices |
| SEC XBRL frames API (one concept for all filers per calendar period) (`sec_frames`) | CONTEXT | public HTTPS API data.sec.gov/api/xbrl/frames; declared User-Agent; free; public domain (US government) | all XBRL filers; annual frames CYyyyy (calendar-aligned, fiscal years mapped to the nearest calendar year) | as filed | latest filed value per entity and frame (restatements replace earlier values) | 2009+ | approximate, latest-value-only: used for a PRELIMINARY finder rank; shortlisted names are re-fetched in full (companyfacts, point-in-time) before any scoring that is shown as a result |
| External research and news (owner-entered) (`external_research`) | CONTEXT | manual entry with URL (`eqm market note`); free/owner; per publisher | whatever the owner enters | publication time as entered | n/a | n/a | claims count as facts only when verified against an ingested primary passage or fact; no licensed news feed is integrated |

| data class | may be used for | not equivalent to | never infer |
|---|---|---|---|
| `PRICE_RETURN` — total or price return of a stock/ETF/index | ATTRIBUTION, CONTEXT | — | causation; buy signal from strength; sell signal from weakness |
| `TRADING_VOLUME` — shares/dollars traded | CONTEXT, LIQUIDITY | ETF_FUND_FLOW | investor flows; conviction |
| `ETF_FUND_FLOW` — net creations/redemptions of ETF shares | CONTEXT | TRADING_VOLUME | forecast |
| `IMPLIED_VOL_INDEX` — option-implied volatility index (e.g. VIX) | CONTEXT | REALIZED_VOL | probability of a business outcome; directional forecast |
| `REALIZED_VOL` — historical volatility of returns | ATTRIBUTION, CONTEXT | IMPLIED_VOL_INDEX | forecast |
| `OPTIONS_ACTIVITY` — options volume / open interest / put-call data | CONTEXT | — | complete investor intent; unhedged directional exposure; bearish conviction from puts |
| `SHORT_INTEREST` — outstanding short position at settlement | CONTEXT, RESEARCH_TRIGGER | SHORT_SALE_VOLUME | sell signal; certain decline |
| `SHORT_SALE_VOLUME` — daily volume of trades executed as short sales | CONTEXT | SHORT_INTEREST | outstanding short interest; bearish conviction; sell signal |
| `FUTURES_PRICE` — futures settlement price | CONTEXT, EXPOSURE_TRIGGER | SPOT_PRICE | unbiased forecast of the future spot price |
| `SPOT_PRICE` — spot commodity price | CONTEXT, EXPOSURE_TRIGGER | FUTURES_PRICE | — |
| `FUTURES_POSITIONING` — futures open interest / trader positioning | CONTEXT | — | directional forecast |
| `INTEREST_RATE` — government yields and policy rates | CONTEXT, EXPOSURE_TRIGGER, VALUATION_INPUT | — | — |
| `CREDIT_SPREAD` — corporate bond option-adjusted spreads | CONTEXT, EXPOSURE_TRIGGER | — | — |
| `INFLATION` — price indices | CONTEXT, EXPOSURE_TRIGGER | — | — |
| `EMPLOYMENT` — labour-market statistics | CONTEXT, EXPOSURE_TRIGGER | — | — |
| `GROWTH` — activity indices (industrial production etc.) | CONTEXT, EXPOSURE_TRIGGER | — | — |
| `FX` — currency indices | CONTEXT, EXPOSURE_TRIGGER | — | — |
| `FILING_EVENT` — SEC filing (8-K item, 10-K/10-Q) | CONTEXT, EVENT, RESEARCH_TRIGGER | — | — |
| `NEWS_CLAIM` — claim from news/external research | CONTEXT, RESEARCH_TRIGGER | — | verified fact unless checked against a primary source |

## Series and instruments wired in this build

| key | name | data class | frequency | publication-time rule |
|---|---|---|---|---|
| `fred:DGS10` | 10-year Treasury yield | INTEREST_RATE | DAILY | NEXT_SESSION (estimated) |
| `fred:DGS2` | 2-year Treasury yield | INTEREST_RATE | DAILY | NEXT_SESSION (estimated) |
| `fred:T10Y2Y` | 10y-2y Treasury spread | INTEREST_RATE | DAILY | NEXT_SESSION (estimated) |
| `fred:DFF` | Effective federal funds rate | INTEREST_RATE | DAILY | NEXT_SESSION (estimated) |
| `fred:BAMLH0A0HYM2` | ICE BofA US High Yield OAS | CREDIT_SPREAD | DAILY | NEXT_SESSION (estimated) |
| `fred:BAMLC0A0CM` | ICE BofA US Corporate (IG) OAS | CREDIT_SPREAD | DAILY | NEXT_SESSION (estimated) |
| `fred:CPIAUCSL` | CPI, all urban consumers (SA index) | INFLATION | MONTHLY | CPI (estimated) |
| `fred:UNRATE` | Unemployment rate | EMPLOYMENT | MONTHLY | EMPLOYMENT (estimated) |
| `fred:PAYEMS` | Nonfarm payrolls (thousands) | EMPLOYMENT | MONTHLY | EMPLOYMENT (estimated) |
| `fred:INDPRO` | Industrial production index | GROWTH | MONTHLY | MONTHLY_MID (estimated) |
| `fred:RSAFS` | Retail sales (advance, $m) | GROWTH | MONTHLY | MONTHLY_MID (estimated) |
| `fred:DTWEXBGS` | Nominal broad US dollar index | FX | DAILY | NEXT_SESSION (estimated) |
| `fred:DCOILWTICO` | WTI crude oil spot | SPOT_PRICE | DAILY | NEXT_SESSION (estimated) |
| `finra_si:<SYMBOL>` | short interest (twice monthly) | SHORT_INTEREST | SEMIMONTHLY | settlement + 8 sessions, 18:00 ET (estimated) |
| `finra_shvol:<SYMBOL>` | short-sale share of FINRA-reported volume | SHORT_SALE_VOLUME | DAILY | next business day 08:00 ET |

Reference ETFs: SPY, QQQ. Sector ETFs: Technology → XLK, Financials → XLF, Energy → XLE, Health Care → XLV, Consumer Discretionary → XLY, Consumer Staples → XLP, Industrials → XLI, Materials → XLB, Utilities → XLU, Real Estate → XLRE, Communication Services → XLC.

Industry benchmarks (SIC range → ETF): 3674-3674 → SMH, 7370-7379 → IGV, 2833-2836 → XBI, 1311-1389 → XOP, 1531-1531 → XHB, 4512-4522 → JETS, 5200-5999 → XRT, 6020-6036 → KBE, 6311-6411 → KIE, 3841-3845 → IHI, 4011-4731 → IYT, 4810-4899 → IYZ, 2000-2099 → PBJ, 3710-3716 → CARZ.

Market proxies (via the price provider): ^VIX (CBOE 30-day S&P 500 implied volatility index; IMPLIED_VOL_INDEX), ^VIX3M (CBOE 3-month S&P 500 implied volatility index; IMPLIED_VOL_INDEX), TLT (20+ year Treasury bond ETF; PRICE_RETURN), HYG (High-yield corporate bond ETF; PRICE_RETURN), CL=F (WTI crude oil front-month futures (continuous); FUTURES_PRICE), HG=F (COMEX copper front-month futures (continuous); FUTURES_PRICE).

## Exposure factors and their indicators

| factor | 'rising' means | default indicators |
|---|---|---|
| RATES | government yields rise | fred:DGS10, fred:DGS2 |
| REFINANCING | the company's cost of refinancing debt rises | fred:DGS10, fred:BAMLH0A0HYM2 |
| CREDIT_CONDITIONS | credit spreads widen / lending tightens | fred:BAMLH0A0HYM2, fred:BAMLC0A0CM |
| CONSUMER_SPENDING | consumer spending strengthens | fred:UNRATE, fred:RSAFS |
| ENTERPRISE_SPENDING | business investment/IT/industrial spending strengthens | fred:INDPRO |
| INFLATION_INPUT_COSTS | input costs / inflation rise | fred:CPIAUCSL |
| EMPLOYMENT | the labour market strengthens | fred:UNRATE, fred:PAYEMS |
| COMMODITY_OIL | oil prices rise | fred:DCOILWTICO |
| COMMODITY_COPPER | copper prices rise | px:HG=F |
| FX_USD | the US dollar strengthens | fred:DTWEXBGS |
| GEOGRAPHY | conditions in a named region deteriorate | none — monitored through filings/research |
| REGULATION | regulatory pressure increases | none — monitored through filings/research |
| CUSTOMER_CONCENTRATION | a major customer weakens or leaves | none — monitored through filings/research |
| SUPPLIER_CONCENTRATION | a critical supplier is disrupted | none — monitored through filings/research |

## Revisions and historical availability

- Every market observation is stored with the time it became public. A different value later reported for the same period is a
  new row; earlier as-of queries keep the earlier value (tested).
- FRED without an API key returns only the current vintage. History loaded by the first refresh is labelled
  `CURRENT_VINTAGE_BACKFILL` and may contain later revisions; the snapshot lists such series under "revision caveat". Real vintages
  accumulate from the first refresh onward. ALFRED vintages would need a FRED API key (deferred).
- Release times for FRED/FINRA are estimated from documented lags and capped at retrieval time.
- Yahoo prices are not versioned; provider corrections overwrite nothing (new bars only) and are not detectable.
- Yahoo returns no history for delisted symbols and adjusts history for every later split: historical windows must be requested through today (selloff study) or prices will carry later reverse splits.
