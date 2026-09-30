"""Synthetic, clearly labelled FIXTURE data for demos and tests.

Nothing here is market evidence. Companies are fictional (tickers ``ZZ*``, names prefixed
"FIXTURE"); the portfolio kind is FIXTURE so it can never mix with actual holdings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .app import App
from .data import calendar as cal
from .data.prices import Action, Bar, PriceFetch, record_check, store_fetch
from .data.securities import get_or_create_issuer, register_security
from .db.core import insert
from .ledger.store import NewEvent, create_account, create_portfolio, record_events
from .research.fundamentals import FactView, add_fact
from .research.thesis import ConditionIn, MilestoneIn, ThesisContent, approve_version, create_version
from .util import new_id, ny_datetime
from .valuation.builder import build_scenarios
from .valuation.dcf import run_dcf
from .valuation.store import approve_valuation, create_valuation
from .decisions.recommend import set_watchlist
from .market.exposures import approve_profile, create_profile, draft_default_profile

D = Decimal


@dataclass
class FixtureCo:
    symbol: str
    name: str
    sic: str
    margins: list[Decimal]
    price_to_base: Decimal | None      # final price as a multiple of base value
    position_usd: Decimal              # 0 => watchlist only
    approve_valuation: bool = True
    watchlist: bool = False


COMPANIES = [
    FixtureCo("ZZADD", "FIXTURE Addco Software", "7372", [D("0.22"), D("0.23"), D("0.24"), D("0.25")], D("0.60"), D("4000")),
    FixtureCo("ZZHLD", "FIXTURE Holdco Beverages", "2080", [D("0.18")] * 4, D("0.90"), D("8000")),
    FixtureCo("ZZTRM", "FIXTURE Trimco Machinery", "3560", [D("0.20")] * 4, D("1.30"), D("5000")),
    FixtureCo("ZZEXT", "FIXTURE Exitco Retail", "5940", [D("0.20"), D("0.18"), D("0.05"), D("0.04")], D("0.50"), D("6000")),
    FixtureCo("ZZREV", "FIXTURE Reviewco Telecom", "4813", [D("0.21")] * 4, D("0.70"), D("7000"), approve_valuation=False),
    FixtureCo("ZZNEW", "FIXTURE Newco Instruments", "3570", [D("0.24")] * 4, D("0.65"), D("0"), watchlist=True),
]

AS_OF = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)
FY_ENDS = [date(2023, 6, 30), date(2024, 6, 30), date(2025, 6, 30), date(2026, 6, 30)]


def _facts(app: App, iss: str, cik: str, margins: list[Decimal]) -> None:
    rev = D(1000_000_000)
    for i, end in enumerate(FY_ENDS):
        start = date(end.year - 1, 7, 1)
        pub = ny_datetime(end + timedelta(days=46), 16, 5)          # 10-K accepted after the close
        acc = f"FIXTURE-{cik}-{end.year}"
        oi = rev * margins[i]
        pretax = oi - D(30_000_000)
        vals = {"revenue": rev, "operating_income": oi, "net_income": pretax * D("0.79"), "pretax_income": pretax,
                "income_tax": pretax * D("0.21"), "cfo": oi * D("1.1"), "capex": rev * D("0.04"), "dna": rev * D("0.03"),
                "sbc": rev * D("0.01"), "interest_expense": D(30_000_000), "buybacks": D(20_000_000),
                "shares_diluted_weighted": D(100_000_000)}
        for concept, v in vals.items():
            add_fact(app, iss, concept, v, start=start, end=end, public_at=pub, accession=acc, fiscal_year=end.year,
                     fiscal_period="FY")
        for concept, v in {"cash": D(500_000_000), "long_term_debt": D(800_000_000), "total_equity": D(1_500_000_000),
                           "shares_outstanding": D(100_000_000)}.items():
            add_fact(app, iss, concept, v, start=None, end=end, public_at=pub, accession=acc)
        rev *= D("1.08")


def _thesis(symbol: str) -> ThesisContent:
    return ThesisContent(
        business_model=f"{symbol} (FIXTURE) sells a recurring product with modest capital needs.",
        valuation_requires="Market price implies low-single-digit growth and margin compression (illustrative).",
        our_view_differs="We assume margins hold near the 3-year average (illustrative).",
        evidence=[], counterargument="Competition could compress margins faster than assumed.",
        milestones=[MilestoneIn(description="Revenue above $1.3bn by FY2027", concept="revenue", metric="value",
                                comparator=">=", target=D(1_300_000_000), due_date=date(2027, 8, 31))],
        invalidation_conditions=[ConditionIn(description="Operating margin below 10% for 2 consecutive fiscal years",
                                             kind="METRIC", concept="operating_income", metric="margin", comparator="<",
                                             threshold=D("0.10"), consecutive_periods=2, period_basis="FY"),
                                 ConditionIn(description="Loss of the largest customer contract", kind="EVENT")],
        value_realization="Cash generation and buybacks over 3-5 years (illustrative).",
        key_risks=["competition", "input costs"], assumptions=["margins stable"], next_review_date=date(2026, 12, 31))


def build_demo(app: App, *, as_of: datetime = AS_OF, portfolio_name: str = "demo-fixture") -> dict:
    pf = create_portfolio(app, portfolio_name, "FIXTURE", note="Synthetic demonstration data; not market evidence.")
    acct = create_account(app, pf, "fixture-brokerage")
    session = cal.latest_completed_session(as_of)
    buy_day = date(2026, 6, 1)
    events = [NewEvent("DEPOSIT", date(2026, 5, 29), amount=D(100000), note="FIXTURE deposit")]
    out = {"portfolio_id": pf, "account_id": acct, "securities": {}}
    for i, co in enumerate(COMPANIES):
        cik = f"99{i:08d}"
        iss = get_or_create_issuer(app.conn, app.now_iso(), name=co.name, cik=cik, sic=co.sic)
        sid = register_security(app.conn, app.now_iso(), co.symbol, security_type="COMMON", issuer_id=iss, source="fixture")
        _facts(app, iss, cik, co.margins)
        fv = FactView(app, iss, as_of)
        scen = build_scenarios(fv, app.policy.valuation)
        base = run_dcf(scen["base"]).value_per_share
        price = (base * co.price_to_base).quantize(D("0.01"))
        buy_px = (base * D("0.75")).quantize(D("0.01"))
        bars = [Bar(d, buy_px) for d in cal.sessions_between(buy_day, session - timedelta(days=7))]
        bars += [Bar(d, price) for d in cal.sessions_between(session - timedelta(days=6), session)]
        store_fetch(app, sid, PriceFetch(bars), "fixture")
        record_check(app, "fixture", iss, "FILINGS", True, "FIXTURE", None, None)
        tv = create_version(app, sid, _thesis(co.symbol), change_reason="initial thesis", author="FIXTURE", label="FIXTURE",
                            as_of=as_of)
        approve_version(app, tv, note="FIXTURE approval")
        vid = create_valuation(app, sid, scen, evidence_as_of=as_of, author="FIXTURE", label="FIXTURE",
                               change_reason="fixture valuation")
        if co.approve_valuation:
            approve_valuation(app, vid, downside_reviewed=True, note="FIXTURE approval")
        if co.position_usd:
            qty = (co.position_usd / buy_px).quantize(D("0.000001"))
            events.append(NewEvent("BUY", buy_day, sid, quantity=qty, price=buy_px, fees=D(0), note="FIXTURE buy"))
        ev = create_profile(app, sid, draft_default_profile(app, sid, as_of), change_reason="initial exposure profile",
                            author="FIXTURE", label="FIXTURE", as_of=as_of)
        approve_profile(app, ev, note="FIXTURE approval")
        if co.watchlist:
            set_watchlist(app, sid, "APPROVED", "FIXTURE watchlist candidate")
        out["securities"][co.symbol] = {"security_id": sid, "issuer_id": iss, "base": base, "price": price}
    # ETF sleeve (bypasses company valuation)
    schg = register_security(app.conn, app.now_iso(), "SCHG", security_type="ETF", source="fixture")
    etf_bars = [Bar(d, D("30.00") + D(i) * D("0.02")) for i, d in enumerate(cal.sessions_between(date(2026, 5, 1), session))]
    store_fetch(app, schg, PriceFetch(etf_bars, [Action("CASH_DIVIDEND", date(2026, 9, 24), cash_amount=D("0.05"))]), "fixture")
    events.append(NewEvent("BUY", buy_day, schg, quantity=D(600), price=[b.close for b in etf_bars if b.session_date == buy_day][0],
                           note="FIXTURE ETF buy"))
    res = record_events(app, acct, events, recorded_by="fixture")
    assert not res.rejected, res.rejected
    out["securities"]["SCHG"] = {"security_id": schg}
    return out


# ---------------------------------------------------------------- market-context fixture (synthetic, labelled)
def build_market_fixture(app: App, *, as_of: datetime = AS_OF, rates_1m_change: Decimal = D(0), hy_level: Decimal = D("3.0"),
                         equity_last_month: Decimal = D(0), vix_level: Decimal = D(16), oil_3m: Decimal = D(0),
                         energy_last_month: Decimal = D(0), missing: tuple[str, ...] = (), tag: str = "") -> str:
    """Synthetic reference prices + economic series, then a snapshot. Returns the snapshot id.

    FIXTURE ONLY: smooth deterministic paths with scenario shocks applied to the most recent month/quarter.
    ``tag`` changes nothing economically; it lets tests create a new vintage for the same period.
    """
    import math
    from .market import series as ms
    from .market.snapshot import build_snapshot, ensure_reference_securities
    session = cal.latest_completed_session(as_of)
    refs = ensure_reference_securities(app)
    days = cal.sessions_between(date(2025, 6, 2), session)
    n = len(days)
    for sym, sid in refs.items():
        # fixture-only: replace this fixture's own synthetic reference bars so a new scenario takes effect
        app.conn.execute("DELETE FROM price_bar WHERE security_id=? AND provider='fixture'", (sid,))
        bars = []
        for i, d in enumerate(days):
            if sym == "^VIX":
                v = vix_level if i >= n - 5 else D(16)
            elif sym == "^VIX3M":
                v = D(18)
            elif sym == "HG=F":
                v = D("4.00")
            elif sym == "CL=F":
                v = D(70) * (1 + oil_3m * max(0, i - (n - 63)) / 63)
            else:
                h = sum(map(ord, sym)) % 7
                base = 100 * math.exp(0.0003 * i + 0.01 * math.sin(i / 7 + h))
                shock = equity_last_month if sym not in ("TLT", "HYG") else D(0)
                if sym == "XLE":
                    shock = shock + energy_last_month
                frac = max(0, i - (n - 21)) / 21
                v = D(str(round(base, 4))) * (1 + shock * D(str(frac)))
            bars.append(Bar(d, v.quantize(D("0.0001")), open=v.quantize(D("0.0001")), volume=D(1_000_000)))
        store_fetch(app, sid, PriceFetch(bars), "fixture")
    start = session - timedelta(days=500)
    daily = [d for d in cal.sessions_between(start, session - timedelta(days=1))]
    def ramp(d, base, delta, window):
        k = (session - d).days
        return base + (delta if k <= window else D(0))
    values = {
        "fred:DGS10": [(d, ramp(d, D("4.00"), rates_1m_change, 20)) for d in daily],
        "fred:DGS2": [(d, ramp(d, D("3.80"), rates_1m_change, 20)) for d in daily],
        "fred:T10Y2Y": [(d, D("0.20")) for d in daily],
        "fred:DFF": [(d, D("3.75")) for d in daily],
        "fred:BAMLH0A0HYM2": [(d, ramp(d, D("3.0"), hy_level - D("3.0"), 5)) for d in daily],
        "fred:BAMLC0A0CM": [(d, D("1.0")) for d in daily],
        "fred:DTWEXBGS": [(d, D("120")) for d in daily],
        "fred:DCOILWTICO": [(d, D(70) * (1 + oil_3m * D(str(max(0, 90 - (session - d).days) / 90)))) for d in daily],
    }
    months = []
    m = date(start.year, start.month, 1)
    while m <= session.replace(day=1):
        months.append(m)
        m = (m + timedelta(days=32)).replace(day=1)
    values.update({
        "fred:CPIAUCSL": [(mm, (D(320) * D("1.0025") ** i).quantize(D("0.001"))) for i, mm in enumerate(months)],
        "fred:UNRATE": [(mm, D("4.0")) for mm in months], "fred:PAYEMS": [(mm, D(160000) + i * 100) for i, mm in enumerate(months)],
        "fred:INDPRO": [(mm, D(103)) for mm in months], "fred:RSAFS": [(mm, D(700000) + i * 1000) for i, mm in enumerate(months)],
    })
    for key, vals in values.items():
        if key in missing:
            continue
        ms.store_values(app, ms.SPECS[key], [(d, v.quantize(D("0.0001"))) for d, v in vals], raw_id=None)
    return build_snapshot(app, as_of)
