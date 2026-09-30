"""Source catalog and data-class interpretation rules.

Two registries, both versioned with the code and rendered into SOURCES.md:

``SOURCES``      what each provider offers: availability, cost, licensing, coverage, publication delay,
                 revisions, history, interpretation limits, and its role (DECISION | CONTEXT | DEFERRED | UNAVAILABLE).
``DATA_CLASSES`` what a kind of number may be used for. ``assert_use`` makes misuse a hard error, e.g.
                 short-sale volume cannot stand in for short interest, ETF trading volume is not a fund flow,
                 implied volatility is not a probability of a business outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    categories: tuple[str, ...]
    role: str                      # DECISION (can affect eligibility/review) | CONTEXT | DEFERRED | UNAVAILABLE
    access: str
    cost: str
    licensing: str
    coverage: str
    publication_delay: str
    revisions: str
    history: str
    limits: str
    status_note: str = ""


SOURCES: dict[str, SourceSpec] = {s.id: s for s in [
    SourceSpec("sec_edgar", "SEC EDGAR filings + XBRL facts", ("fundamentals", "disclosures", "events"), "DECISION",
               "public HTTPS API; declared User-Agent with contact email; <=10 req/s", "free", "public domain (US government)",
               "US SEC registrants; XBRL facts ~2009+", "acceptance timestamp (seconds)", "amendments are new filings; kept as new rows",
               "full EDGAR history", "as-filed data; non-GAAP, segments and guidance text are not normalized"),
    SourceSpec("yahoo_chart", "Yahoo Finance chart endpoint (stocks, ETFs, indices such as ^VIX, futures such as CL=F)",
               ("prices", "etf_prices", "volume", "implied_vol_index", "futures_prices"), "DECISION",
               "unofficial, undocumented HTTPS endpoint; rate limited", "free", "no license grant; personal research only",
               "US stocks/ETFs; CBOE indices; front-month continuous futures", "end of day (we wait 2h after close)",
               "split-adjusted closes are un-adjusted by us; occasional provider corrections are not versioned",
               "decades for large ETFs; continuous futures roll without adjustment",
               "volume is trading activity, not investor flows; continuous futures series have roll jumps"),
    SourceSpec("fred", "FRED graph CSV (St. Louis Fed)", ("rates", "inflation", "growth", "employment", "credit", "fx", "commodities"),
               "DECISION", "public CSV download, no key", "free",
               "public; some series (ICE BofA spreads) carry third-party terms and limited history",
               "US macro and market series", "daily market series ~1 business day; CPI/payrolls ~2-6 weeks after period",
               "CURRENT VINTAGE ONLY without an API key: backfilled history may include later revisions (flagged); "
               "revisions seen after first retrieval are stored as new rows", "decades (series-dependent)",
               "release dates are estimated from publication-lag rules unless provided",
               "ALFRED vintages need a FRED API key (not configured) -> deferred"),
    SourceSpec("finra_short_interest", "FINRA consolidated short interest (API)", ("short_interest",), "CONTEXT",
               "public API (api.finra.org), no key for this dataset", "free", "FINRA terms of use",
               "exchange-listed and OTC equities", "twice monthly; published ~7-8 business days after settlement (estimated)",
               "revision flag provided by FINRA; revised values stored as new rows", "2020+ via API",
               "a stock of open short positions at settlement date; says nothing about hedging motives; never a sell signal"),
    SourceSpec("finra_regsho_daily", "FINRA Reg SHO daily short-sale volume files", ("short_sale_volume",), "CONTEXT",
               "public flat files (cdn.finra.org), no key", "free", "FINRA terms of use",
               "trades reported to FINRA facilities only (not total consolidated volume)", "next business day",
               "rare; not versioned", "rolling archive",
               "short-SALE VOLUME includes market-maker hedging and is NOT outstanding short interest"),
    SourceSpec("treasury_rates", "US Treasury daily par yield curve CSV", ("rates",), "CONTEXT",
               "public CSV", "free", "public domain", "nominal par yields 1m-30y", "same day after ~15:30 ET",
               "rare corrections", "1990+", "alternative to FRED DGS series; not wired by default"),
    SourceSpec("cftc_cot", "CFTC Commitments of Traders", ("futures_positioning",), "DEFERRED",
               "public text files", "free", "public domain", "futures positioning by trader category (weekly, Tuesday data)",
               "published Friday 15:30 ET for Tuesday data", "occasional", "1986+",
               "open interest/positioning is not a directional forecast; mapping contracts to companies is weak",
               "reachable; deferred until an exposure clearly needs it"),
    SourceSpec("options_chains", "Single-stock options (IV, skew, term structure, volume, open interest)", ("options",),
               "UNAVAILABLE", "no free reliable source (Yahoo options endpoint returns 401)", "paid vendors (e.g. OPRA-derived)",
               "vendor license", "-", "-", "-", "-",
               "activity is not investor intent or unhedged direction; IV is not an objective probability",
               "market-level implied volatility is available via ^VIX / ^VIX3M indices"),
    SourceSpec("etf_flows_holdings", "ETF fund flows, full holdings, breadth", ("etf_flows", "etf_holdings"), "UNAVAILABLE",
               "issuer holdings files exist (e.g. SSGA xlsx) but formats change; flows need paid data", "flows: paid",
               "issuer/vendor terms", "-", "holdings daily; flows daily (vendor)", "-", "-",
               "trading volume is not a fund flow; look-through sector weights of held ETFs stay UNKNOWN",
               "deferred: holdings files could be added for SPY/QQQ/SCHG"),
    SourceSpec("external_research", "External research and news (owner-entered)", ("news", "research"), "CONTEXT",
               "manual entry with URL (`eqm market note`)", "free/owner", "per publisher", "whatever the owner enters",
               "publication time as entered", "n/a", "n/a",
               "claims count as facts only when verified against an ingested primary passage or fact",
               "no licensed news feed is integrated"),
]}


@dataclass(frozen=True)
class DataClassSpec:
    key: str
    description: str
    uses: frozenset[str]                         # CONTEXT, EXPOSURE_TRIGGER, VALUATION_INPUT, RESEARCH_TRIGGER, ATTRIBUTION, LIQUIDITY, EVENT
    not_equivalent_to: tuple[str, ...] = ()
    prohibited_inferences: tuple[str, ...] = field(default_factory=tuple)


DATA_CLASSES: dict[str, DataClassSpec] = {d.key: d for d in [
    DataClassSpec("PRICE_RETURN", "total or price return of a stock/ETF/index", frozenset({"CONTEXT", "ATTRIBUTION"}),
                  (), ("causation", "buy signal from strength", "sell signal from weakness")),
    DataClassSpec("TRADING_VOLUME", "shares/dollars traded", frozenset({"CONTEXT", "LIQUIDITY"}),
                  ("ETF_FUND_FLOW",), ("investor flows", "conviction")),
    DataClassSpec("ETF_FUND_FLOW", "net creations/redemptions of ETF shares", frozenset({"CONTEXT"}),
                  ("TRADING_VOLUME",), ("forecast",)),
    DataClassSpec("IMPLIED_VOL_INDEX", "option-implied volatility index (e.g. VIX)", frozenset({"CONTEXT"}),
                  ("REALIZED_VOL",), ("probability of a business outcome", "directional forecast")),
    DataClassSpec("REALIZED_VOL", "historical volatility of returns", frozenset({"CONTEXT", "ATTRIBUTION"}),
                  ("IMPLIED_VOL_INDEX",), ("forecast",)),
    DataClassSpec("OPTIONS_ACTIVITY", "options volume / open interest / put-call data", frozenset({"CONTEXT"}),
                  (), ("complete investor intent", "unhedged directional exposure", "bearish conviction from puts")),
    DataClassSpec("SHORT_INTEREST", "outstanding short position at settlement", frozenset({"CONTEXT", "RESEARCH_TRIGGER"}),
                  ("SHORT_SALE_VOLUME",), ("sell signal", "certain decline")),
    DataClassSpec("SHORT_SALE_VOLUME", "daily volume of trades executed as short sales", frozenset({"CONTEXT"}),
                  ("SHORT_INTEREST",), ("outstanding short interest", "bearish conviction", "sell signal")),
    DataClassSpec("FUTURES_PRICE", "futures settlement price", frozenset({"CONTEXT", "EXPOSURE_TRIGGER"}),
                  ("SPOT_PRICE",), ("unbiased forecast of the future spot price",)),
    DataClassSpec("SPOT_PRICE", "spot commodity price", frozenset({"CONTEXT", "EXPOSURE_TRIGGER"}), ("FUTURES_PRICE",), ()),
    DataClassSpec("FUTURES_POSITIONING", "futures open interest / trader positioning", frozenset({"CONTEXT"}),
                  (), ("directional forecast",)),
    DataClassSpec("INTEREST_RATE", "government yields and policy rates", frozenset({"CONTEXT", "EXPOSURE_TRIGGER", "VALUATION_INPUT"}), (), ()),
    DataClassSpec("CREDIT_SPREAD", "corporate bond option-adjusted spreads", frozenset({"CONTEXT", "EXPOSURE_TRIGGER"}), (), ()),
    DataClassSpec("INFLATION", "price indices", frozenset({"CONTEXT", "EXPOSURE_TRIGGER"}), (), ()),
    DataClassSpec("EMPLOYMENT", "labour-market statistics", frozenset({"CONTEXT", "EXPOSURE_TRIGGER"}), (), ()),
    DataClassSpec("GROWTH", "activity indices (industrial production etc.)", frozenset({"CONTEXT", "EXPOSURE_TRIGGER"}), (), ()),
    DataClassSpec("FX", "currency indices", frozenset({"CONTEXT", "EXPOSURE_TRIGGER"}), (), ()),
    DataClassSpec("FILING_EVENT", "SEC filing (8-K item, 10-K/10-Q)", frozenset({"CONTEXT", "EVENT", "RESEARCH_TRIGGER"}), (), ()),
    DataClassSpec("NEWS_CLAIM", "claim from news/external research", frozenset({"CONTEXT", "RESEARCH_TRIGGER"}),
                  (), ("verified fact unless checked against a primary source",)),
]}


class InterpretationError(ValueError):
    pass


def assert_use(data_class: str, use: str) -> None:
    spec = DATA_CLASSES.get(data_class)
    if spec is None:
        raise InterpretationError(f"unknown data class {data_class}")
    if use not in spec.uses:
        raise InterpretationError(f"{data_class} may not be used as {use}; allowed: {sorted(spec.uses)}")


def assert_same_class(provided: str, required: str) -> None:
    """Refuse substitution of one data class for another (e.g. short-sale volume for short interest)."""
    if provided != required:
        extra = ""
        if required in DATA_CLASSES.get(provided, DataClassSpec(provided, "", frozenset())).not_equivalent_to:
            extra = f" ({provided} is explicitly NOT equivalent to {required})"
        raise InterpretationError(f"required {required}, got {provided}{extra}")


def assert_inference_allowed(data_class: str, inference: str) -> None:
    for bad in DATA_CLASSES[data_class].prohibited_inferences:
        if bad.lower() in inference.lower():
            raise InterpretationError(f"{data_class} cannot support the inference '{inference}'")


def sources_markdown() -> str:
    lines = ["| source | role | access / cost / licensing | coverage | publication delay | revisions | history | interpretation limits |",
             "|---|---|---|---|---|---|---|---|"]
    for s in SOURCES.values():
        lines.append(f"| {s.name} (`{s.id}`) | {s.role} | {s.access}; {s.cost}; {s.licensing} | {s.coverage} | "
                     f"{s.publication_delay} | {s.revisions} | {s.history} | {s.limits}{'; ' + s.status_note if s.status_note else ''} |")
    lines += ["", "| data class | may be used for | not equivalent to | never infer |", "|---|---|---|---|"]
    for d in DATA_CLASSES.values():
        lines.append(f"| `{d.key}` — {d.description} | {', '.join(sorted(d.uses))} | {', '.join(d.not_equivalent_to) or '—'} | "
                     f"{'; '.join(d.prohibited_inferences) or '—'} |")
    return "\n".join(lines)
