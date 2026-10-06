"""Post-Selloff Recovery Research (sr-0.1) — offline, credential-free fixtures: a fake EDGAR (search, submissions,
filing documents, index pages, companyfacts) and a fake price source. Every test runs in its own research home."""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from equity_monitor.data.http import ProviderError
from equity_monitor.data.prices import Bar, PriceFetch
from equity_monitor.research.selloff import discovery, packs, pipeline, pricing, records, report, screening
from equity_monitor.research.selloff.home import NotAResearchHome, init_home, open_research
from equity_monitor.research.selloff.timing import event_timing
from equity_monitor.util import Clock

UTC = timezone.utc
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
CIK = "1001"
A1, A2, A3 = "0000000001-21-000001", "0000000001-21-000002", "0000000001-22-000003"
Q1, Q2 = "0000000001-21-000010", "0000000001-21-000011"
ARCH = "https://www.sec.gov/Archives/edgar/data/1001/{acc}/{file}"


def _cover(report_date: str, symbol: str = "ALPH") -> str:
    return (f"<p>FORM 8-K</p><p>Date of Report (Date of earliest event reported): {report_date}</p><p>ALPHA BIO, INC.</p>"
            "<p>(Exact name of registrant as specified in its charter)</p><p>Securities registered pursuant to Section "
            "12(b) of the Act:</p><table><tr><td>Title of each class</td><td>Trading Symbol(s)</td><td>Name of each "
            f"exchange on which registered</td></tr><tr><td>Common Stock</td><td>{symbol}</td><td>The Nasdaq Stock "
            "Market LLC</td></tr></table><p>Indicate by check mark whether the registrant is an emerging growth company.</p>")


DOCS = {
    (A1, "a1.htm"): _cover("March 1, 2021") + "<p>Item 8.01 Other Events. See the press release in Exhibit 99.1.</p>",
    (A1, "a1ex.htm"): ("<p>EXHIBIT 99.1</p><p>Alpha Bio Announces Topline Results from Phase 3 ALPHA-1 Trial</p><p>BOSTON, "
                       "March 1, 2021 (GLOBE NEWSWIRE) -- Alpha Bio, Inc. (Nasdaq: ALPH) today announced that its Phase 3 "
                       "ALPHA-1 trial of AB-101 in disease X did not meet its primary endpoint. AB-101 was generally well "
                       "tolerated. The Company will discontinue development of AB-101 in disease X and plans to report "
                       "data from its Phase 2 program of AB-202 in the second half of 2021.</p>"),
    (A2, "a2.htm"): _cover("March 19, 2021") + ("<p>Item 8.01 Other Events. On March 19, 2021, Alpha Bio presented full "
                                                "results of the Phase 3 ALPHA-1 trial, which did not meet its primary "
                                                "endpoint.</p>"),
    (A3, "a3.htm"): _cover("May 2, 2022") + ("<p>Item 8.01 Other Events. The Phase 2 BETA-2 trial of AB-101 in disease Y "
                                             "did not meet its primary endpoint.</p>"),
    (Q1, "q1.htm"): _cover("") .replace("FORM 8-K", "FORM 10-Q") + "<p>Cash and cash equivalents were $100.0 million.</p>",
    (Q2, "q2.htm"): _cover("").replace("FORM 8-K", "FORM 10-Q") + "<p>Cash was restated to $80.0 million.</p>",
}
FILINGS = [  # accession, form, filing date, acceptance (UTC), primary document, items
    (A1, "8-K", "2021-03-01", "2021-03-01T12:00:00.000Z", "a1.htm", "8.01,9.01"),
    (A2, "8-K", "2021-03-19", "2021-03-19T12:00:00.000Z", "a2.htm", "8.01"),
    (A3, "8-K", "2022-05-02", "2022-05-02T20:30:00.000Z", "a3.htm", "8.01"),
    (Q1, "10-Q", "2021-02-10", "2021-02-10T21:00:00.000Z", "q1.htm", ""),
    (Q2, "10-Q", "2021-05-10", "2021-05-10T21:00:00.000Z", "q2.htm", ""),
    ("0000000001-23-000099", "25-NSE", "2023-01-10", "2023-01-10T15:00:00.000Z", "x.htm", ""),
]


def _companyfacts() -> dict:
    def usd(val, end, filed, accn, start=None, form="10-Q", fy=2020, fp="FY"):
        d = {"end": end, "val": val, "accn": accn, "filed": filed, "form": form, "fy": fy, "fp": fp}
        if start:
            d["start"] = start
        return d
    return {"cik": 1001, "entityName": "Alpha Bio, Inc.", "facts": {
        "us-gaap": {
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                usd(100_000_000, "2020-12-31", "2021-02-10", Q1),
                usd(80_000_000, "2020-12-31", "2021-05-10", Q2)]}},          # later restatement of the same period
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                usd(-60_000_000, "2020-12-31", "2021-02-10", Q1, start="2020-01-01", form="10-K")]}}},
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            usd(10_000_000, "2021-02-05", "2021-02-10", Q1)]}}}}}


def _fetch(url: str) -> bytes:
    if "submissions" in url:
        if "CIK0000001001" not in url:
            raise ProviderError("SEC_EDGAR", "HTTP 404", status=404)
        cols = {k: [] for k in ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "primaryDocument", "items",
                                "reportDate")}
        for acc, form, fd, at, prim, items in FILINGS:
            for k, v in zip(cols, (acc, form, fd, at, prim, items, "")):
                cols[k].append(v)
        return json.dumps({"name": "Alpha Bio, Inc.", "sic": "2834", "sicDescription": "Pharmaceutical Preparations",
                           "tickers": ["NEWTICK"], "formerNames": [], "filings": {"recent": cols, "files": []}}).encode()
    if url.endswith("-index.html"):
        if A1.replace("-", "") in url:
            return (b"<table><tr><td>1</td><td>8-K</td><td>a1.htm</td><td>8-K</td></tr><tr><td>2</td><td>EX-99.1</td>"
                    b"<td>a1ex.htm</td><td>EX-99.1</td></tr></table>")
        return b"<table></table>"
    for (acc, fn), html in DOCS.items():
        if url == ARCH.format(acc=acc.replace("-", ""), file=fn):
            return f"<html><body>{html}</body></html>".encode()
    raise ProviderError("SEC_EDGAR", f"HTTP 404 for {url}", status=404)


def _search(phrase, start, end, frm):
    hits = []
    if phrase == '"did not meet its primary endpoint"':
        for acc, _f, fd, _a, prim, items in FILINGS[:3]:
            if start <= fd <= end:
                f = "a1ex.htm" if acc == A1 else prim
                hits.append({"_id": f"{acc}:{f}", "_source": {
                    "adsh": acc, "ciks": ["0000001001"], "file_date": fd, "form": "8-K", "items": items.split(","),
                    "sics": ["2834"], "display_names": ["Alpha Bio, Inc.  (NEWTICK)  (CIK 0000001001)"]}})
    return {"hits": {"total": {"value": len(hits), "relation": "eq"}, "hits": hits}}


def _bars(start: date, end: date, drop_on: date | None = None, base=D(10)):
    from equity_monitor.data import calendar as cal
    out, d, px = [], cal.session_on_or_after(start), base
    while d <= end:
        if drop_on and d == drop_on:
            px = px * D("0.4")
        out.append(Bar(d, px, open=px, volume=D(100000)))
        d = cal.next_session(d)
    return out


def _history(names: dict | None = None, gone: tuple = ("GONE",)):
    names = names or {}

    def history(sym, start, end):
        if sym in gone:
            raise ProviderError("yahoo_chart", "provider error: No data found, symbol may be delisted")
        drop = date(2021, 3, 1) if sym == "ALPH" else None
        bars = _bars(date(2019, 1, 2), min(end, date(2023, 12, 29)), drop)
        meta = {"chart": {"result": [{"meta": {"symbol": sym, "longName": names.get(sym, {"ALPH": "Alpha Bio, Inc.",
                "SPY": "SPDR S&P 500", "XBI": "SPDR S&P Biotech"}.get(sym, sym)), "firstTradeDate": 0}}]}}
        return PriceFetch(bars, [], json.dumps(meta).encode(), f"fake://{sym}", "application/json")
    return history


def _facts(_iid):
    return json.dumps(_companyfacts()).encode(), None


DECISIONS = [
    {"accession": A1, "decision": "INCLUDED", "category": "PRIMARY_ENDPOINT_FAILURE", "phase": "Phase 3", "drug": "AB-101",
     "indication": "disease X", "trial_name": "ALPHA-1", "partner_run": False, "flags": [],
     "quote": "Phase 3 ALPHA-1 trial of AB-101 in disease X did not meet its primary endpoint",
     "phase_quote": "Phase 3 ALPHA-1 trial", "reason": "own Phase 3 failure"},
    {"accession": A2, "decision": "INCLUDED", "category": "PRIMARY_ENDPOINT_FAILURE", "phase": "Phase 3", "drug": "AB-101",
     "indication": "disease X", "trial_name": "ALPHA-1", "partner_run": False, "flags": [],
     "quote": "presented full results of the Phase 3 ALPHA-1 trial, which did not meet its primary endpoint",
     "phase_quote": "Phase 3 ALPHA-1 trial", "reason": "same readout, full data"},
    {"accession": A3, "decision": "INCLUDED", "category": "PRIMARY_ENDPOINT_FAILURE", "phase": "Phase 2", "drug": "AB-101",
     "indication": "disease Y", "trial_name": "BETA-2", "partner_run": False, "flags": [],
     "quote": "The Phase 2 BETA-2 trial of AB-101 in disease Y did not meet its primary endpoint.",
     "phase_quote": "Phase 2 BETA-2 trial", "reason": "same drug, different trial"},
]


@pytest.fixture
def rh(tmp_path):
    app = init_home(tmp_path / "research", clock=Clock(NOW), portfolio_home=tmp_path / "portfolio")
    discovery.run_discovery(app, _search)
    return app


def _screened(app):
    screening.record(app, DECISIONS, "test screener", _fetch)
    return app


def _ran(app, history=None):
    _screened(app)
    pipeline.run(app, fetch=_fetch, history=history or _history(), fetch_facts=_facts, today=date(2026, 10, 2))
    return app


def _event(app, trial):
    return dict(app.conn.execute("SELECT * FROM sr_event WHERE trial_id=?", (trial,)).fetchone())


# ------------------------------------------------------------------ isolation from the portfolio
def test_research_home_never_touches_the_portfolio_database(tmp_path):
    from equity_monitor.app import open_app
    from equity_monitor.ledger.store import create_portfolio
    pf_home = tmp_path / "portfolio"
    pf = open_app(pf_home, clock=Clock(NOW))
    create_portfolio(pf, "side", "ACTUAL")
    pf.conn.close()
    db = pf_home / "equity_monitor.sqlite"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(NotAResearchHome, match="portfolio data home"):
        init_home(pf_home, clock=Clock(NOW), portfolio_home=pf_home)
    with pytest.raises(NotAResearchHome, match="holds portfolio data"):
        init_home(pf_home, clock=Clock(NOW), portfolio_home=tmp_path / "elsewhere")
    with pytest.raises(NotAResearchHome, match="not a selloff research home"):
        open_research(pf_home, clock=Clock(NOW))
    app = init_home(tmp_path / "research", clock=Clock(NOW), portfolio_home=pf_home)
    discovery.run_discovery(app, _search)
    _ran(app)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before                       # byte-identical
    pf = open_app(pf_home, clock=Clock(NOW))
    assert pf.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'sr_%'").fetchone()[0] == 0
    for t in ("recommendation", "watchlist_entry"):
        assert app.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0          # research creates no decisions


# ------------------------------------------------------------------ discovery, screening, idempotency, audit history
def test_discovery_is_logged_ordered_and_idempotent(rh):
    n = rh.conn.execute("SELECT COUNT(*) FROM sr_search").fetchone()[0]
    assert n == 5 * 8 + 5 * 3                                                          # every phrase x year, logged
    again = discovery.run_discovery(rh, _search)
    assert again["queries"] == 0 and rh.conn.execute("SELECT COUNT(*) FROM sr_search").fetchone()[0] == n
    p = discovery.pool(rh)
    assert [r["accession"] for r in p] == sorted([A1, A2, A3], key=lambda a: discovery.sample_key("sr-0.1", a))
    assert all("NEWTICK" in r["display_name"] for r in p)                              # today's ticker, never used
    with pytest.raises(Exception, match="append-only"):
        rh.conn.execute("DELETE FROM sr_search")


def test_screening_quotes_must_be_verbatim_and_cohort_rules_enforced(rh):
    bad = dict(DECISIONS[0], quote="AB-101 met its primary endpoint")                  # not in the filing
    r = screening.record(rh, [bad], "t", _fetch)[0]
    assert r["decision"] == "UNRESOLVED" and "not found verbatim" in r["reason"]
    p1 = dict(DECISIONS[2], accession=A3, phase="Phase 1")
    assert screening.record(rh, [p1], "t", _fetch)[0]["decision"] == "UNRESOLVED"      # phase outside the cohort
    ok = screening.record(rh, [DECISIONS[0]], "t", _fetch)[0]
    assert ok["decision"] == "INCLUDED" and ok["quote_verified"] == 1
    hist = rh.conn.execute("SELECT decision FROM sr_screen WHERE accession=? ORDER BY rowid", (A1,)).fetchall()
    assert [h[0] for h in hist] == ["UNRESOLVED", "INCLUDED"]                         # corrections append; history kept


def test_duplicate_announcements_merge_and_repeated_trials_are_flagged(rh):
    _screened(rh)
    out = screening.build_events(rh, _fetch)
    assert out == {"created": 2, "existing": 0, "events": 2}
    a = _event(rh, "ALPHA-1")
    assert a["event_key"] == "1001|ab-101|disease x|phase 3"                           # no NCT: drug + indication + phase
    assert json.loads(a["accessions_json"]) == [A1, A2]                                # earliest filing first
    assert a["public_earliest"].startswith("2021-03-01T05:00")                         # 00:00 New York, from A1
    b = _event(rh, "BETA-2")
    assert b["repeat_of"] == a["id"]                                                   # same drug, different trial
    assert screening.build_events(rh, _fetch)["created"] == 0                          # idempotent
    with pytest.raises(Exception, match="append-only"):
        rh.conn.execute("UPDATE sr_event SET drug='x'")


# ------------------------------------------------------------------ timing
def test_date_only_sources_never_get_intraday_precision():
    t = event_timing(accepted=datetime(2021, 3, 1, 12, 0, tzinfo=UTC), filing_date="2021-03-01", dateline="2021-03-01")
    assert t.precision == "DATE_ONLY" and t.public_earliest == datetime(2021, 3, 1, 5, 0, tzinfo=UTC)   # 00:00 New York
    assert t.pre_session == date(2021, 2, 26) and t.measurement_session == date(2021, 3, 1)
    # release dated the day BEFORE the 8-K: the window starts before that date
    t2 = event_timing(accepted=datetime(2020, 2, 26, 13, 54, tzinfo=UTC), filing_date="2020-02-26", dateline="2020-02-25")
    assert t2.pre_session == date(2020, 2, 24) and t2.measurement_session == date(2020, 2, 26)
    # no acceptance time at all: the latest possible time is the END of the filing date
    t3 = event_timing(accepted=None, filing_date="2020-08-31")
    assert t3.precision == "DATE_ONLY" and t3.measurement_session == date(2020, 9, 1)


def test_entry_is_after_the_completed_decline_is_observable():
    t = event_timing(accepted=datetime(2022, 5, 2, 20, 30, tzinfo=UTC), filing_date="2022-05-02")   # 16:30 New York
    assert t.precision == "EXACT" and t.pre_session == date(2022, 5, 2)                 # that day's close is pre-event
    assert t.measurement_session == date(2022, 5, 3)
    assert t.decision_cutoff == datetime(2022, 5, 3, 22, 0, tzinfo=UTC)                  # close 20:00Z + 120 min
    assert t.entry_session == date(2022, 5, 4)                                         # next open after the cutoff
    t2 = event_timing(accepted=datetime(2021, 3, 1, 12, 0, tzinfo=UTC), filing_date="2021-03-01")  # 07:00, pre-open
    assert t2.measurement_session == date(2021, 3, 1) and t2.entry_session == date(2021, 3, 2)


# ------------------------------------------------------------------ identity and tickers
def test_ticker_comes_from_the_filing_and_identity_is_guarded(rh):
    _ran(rh, _history(names={"ALPH": "Totally Different Holdings Corp"}))
    a = _event(rh, "ALPHA-1")
    assert a["ticker_at_time"] == "ALPH" and a["cik"] == "1001"                         # not today's NEWTICK
    pc = rh.conn.execute("SELECT status, detail_json FROM sr_price_check WHERE subject=?", (a["id"],)).fetchone()
    assert pc[0] == "IDENTITY_UNVERIFIED" and "does not match" in json.loads(pc[1])["reason"]
    assert rh.conn.execute("SELECT COUNT(*) FROM security WHERE symbol='ALPH@1001'").fetchone()[0] == 0  # not stored
    el = json.loads(rh.conn.execute("SELECT detail_json FROM sr_eligibility WHERE event_id=?", (a["id"],)).fetchone()[0])
    assert el["undetermined"] and el["event"]["status"] == "NO_DATA"


def test_prices_are_requested_through_today_so_later_splits_are_reversed(rh):
    """Shared-adapter limitation found live: the provider adjusts history for EVERY later split, but only splits inside
    the requested range can be reversed. Event histories are therefore always requested through today."""
    asked = []
    base = _history()

    def spy(sym, start, end):
        asked.append((sym, end))
        return base(sym, start, end)
    _ran(rh, spy)
    assert all(end == date(2026, 10, 2) for sym, end in asked if sym == "ALPH")


def test_research_securities_are_keyed_by_ticker_and_cik(rh):
    _ran(rh)
    assert rh.conn.execute("SELECT COUNT(*) FROM security WHERE symbol='ALPH@1001'").fetchone()[0] == 1
    assert rh.conn.execute("SELECT COUNT(*) FROM security WHERE symbol='ALPH'").fetchone()[0] == 0   # reuse-safe


# ------------------------------------------------------------------ point in time: documents and restatements
def test_documents_after_the_cutoff_are_excluded(rh):
    _ran(rh)
    a = _event(rh, "ALPHA-1")
    docs = {d["accession_no"] for d in records.pit_documents(rh, a)}
    assert Q1 in docs and A1 in docs and Q2 not in docs and A3 not in docs
    gaps = [g[0] for g in rh.conn.execute("SELECT description FROM sr_gap WHERE event_id=? AND kind='EXCLUDED_AFTER_CUTOFF'",
                                          (a["id"],))]
    assert any(Q2 in g for g in gaps)
    pack, _h = packs.build_pack(rh, a["id"])
    assert "restated to $80.0 million" not in pack and "Cash and cash equivalents were $100.0 million" in pack
    from equity_monitor.research.evidence import ClaimIn, Citation, verify_claim
    from equity_monitor.util import parse_utc
    q2_passage = rh.conn.execute("SELECT p.id FROM document_passage p JOIN source_document d ON d.id=p.document_id "
                                 "WHERE d.accession_no=?", (Q2,)).fetchone()
    if q2_passage:                                     # stored for the later event; never citable for this one
        v = verify_claim(rh, ClaimIn(text="Cash was restated.", claim_type="FACT",
                                     citations=[Citation(passage_id=q2_passage[0], quote="restated")]),
                         a["issuer_id"], parse_utc(a["decision_cutoff"]))
        assert v.status == "FAILED"


def test_later_restatements_are_recorded_but_never_used(rh):
    _ran(rh)
    a = _event(rh, "ALPHA-1")
    cash = rh.conn.execute("SELECT value_num, label FROM sr_fact WHERE event_id=? AND field='cash_and_equivalents'",
                           (a["id"],)).fetchone()
    assert D(cash[0]) == D(100_000_000) and cash[1].startswith("LATER_REVISED")          # as filed by the cutoff


# ------------------------------------------------------------------ missing data
def test_missing_financial_values_are_unknown_never_zero(rh):
    _ran(rh)
    a = _event(rh, "ALPHA-1")
    f = {r["field"]: dict(r) for r in rh.conn.execute("SELECT * FROM sr_fact WHERE event_id=?", (a["id"],))}
    assert f["revenue_latest_fiscal_year"]["value_num"] is None and "unknown" in f["revenue_latest_fiscal_year"]["null_reason"]
    assert f["long_term_investments"]["value_num"] is None and "not zero" in f["long_term_investments"]["null_reason"]
    run = f["historical_cash_use_runway_months"]
    assert run["kind"] == "ASSUMPTION" and D(run["value_num"]) == D("20.0") and "NOT a forecast" in run["note"]
    assert "UNSUPPORTED_VALUATION" in f["valuation_method"]["value_text"]
    with pytest.raises(ValueError, match="never zero"):
        records.add_fact(rh, a["id"], "C_FINANCING", "x", "FACT")


def test_stale_balances_are_unknown_not_current(rh):
    """Found live: a debt tag last reported eight years before the event was returned as the current balance."""
    from equity_monitor.research.fundamentals import add_fact as add_xbrl
    _screened(rh)
    screening.build_events(rh, _fetch)
    a = _event(rh, "ALPHA-1")
    add_xbrl(rh, a["issuer_id"], "long_term_debt_total", D(50_000_000), start=None, end=date(2015, 12, 31),
             public_at=datetime(2016, 2, 20, tzinfo=UTC), accession="old-10k")
    records.ingest_financials(rh, a, _facts)
    records.financing_record(rh, a)
    f = rh.conn.execute("SELECT value_num, null_reason, verification_status FROM sr_fact WHERE event_id=? AND "
                        "field='long_term_debt'", (a["id"],)).fetchone()
    assert f[0] is None and f[2] == "STALE" and "2015-12-31" in f[1]


def test_missing_and_delisted_price_histories_remain_visible(rh):
    _ran(rh, _history(gone=("ALPH",)))
    rows = report.coverage_rows(rh)
    assert len(rows) == 2                                                             # nothing silently dropped
    r = next(x for x in rows if x["trial"] == "ALPHA-1")
    assert r["price_check"] == "NO_DATA" and r["eligibility"] == "UNDETERMINED" and r["decline"] is None
    assert r["h252"] == "NO_PRICE_SOURCE" and "delisted" in r["unresolved"]
    assert r["outcome_status_evidence"] == "DELISTING_OR_DEREGISTRATION_EVIDENCE"       # Form 25-NSE after the event


def test_renamed_company_is_priced_through_its_cik_with_the_identity_guard(rh):
    """Ticker change: the ticker at the time is gone, the SAME CIK now trades under a new symbol (history kept)."""
    _ran(rh, _history(names={"NEWTICK": "Alpha Bio Holdings, Inc."}, gone=("ALPH",)))
    a = _event(rh, "ALPHA-1")
    pc = rh.conn.execute("SELECT status, symbol, detail_json FROM sr_price_check WHERE subject=?", (a["id"],)).fetchone()
    d = json.loads(pc[2])
    assert pc[0] == "OK" and pc[1] == "NEWTICK" and d["symbol_source"] == "CURRENT_TICKER_SAME_CIK"
    assert d["ticker_at_time"] == "ALPH" and "delisted" in d["ticker_at_time_result"]


def test_current_ticker_of_another_company_is_never_used(tmp_path):
    app = init_home(tmp_path / "r", clock=Clock(NOW), portfolio_home=tmp_path / "p")
    discovery.run_discovery(app, _search)
    _ran(app, _history(names={"NEWTICK": "Unrelated Mining Corp"}, gone=("ALPH",)))
    a = _event(app, "ALPHA-1")
    assert app.conn.execute("SELECT status FROM sr_price_check WHERE subject=?", (a["id"],)).fetchone()[0] == "NO_DATA"


def test_eligibility_uses_the_window_and_outcome_audit_has_no_returns(rh):
    _ran(rh)
    a = _event(rh, "ALPHA-1")
    el = json.loads(rh.conn.execute("SELECT detail_json FROM sr_eligibility WHERE event_id=?", (a["id"],)).fetchone()[0])
    assert D(el["decline"]) == D("-0.6") and el["pre_event_market_cap"] == "100000000"
    assert rh.conn.execute("SELECT status FROM sr_eligibility WHERE event_id=?", (a["id"],)).fetchone()[0] == "ELIGIBLE"
    oa = rh.conn.execute("SELECT detail_json FROM sr_outcome_audit WHERE event_id=?", (a["id"],)).fetchone()[0]
    assert "return" not in oa.lower() and json.loads(oa)["stock"]["horizons"]["252"] == "AVAILABLE"


# ------------------------------------------------------------------ facts vs judgments; LLM labels
def test_facts_assumptions_and_opinions_stay_separate_and_judgments_are_retrospective(rh, tmp_path):
    _ran(rh)
    a = _event(rh, "ALPHA-1")
    kinds = {r[0] for r in rh.conn.execute("SELECT DISTINCT kind FROM sr_fact WHERE event_id=?", (a["id"],))}
    assert kinds <= {"FACT", "ASSUMPTION"}                                            # no opinions without a judgment
    stated = rh.conn.execute("SELECT verification_status FROM sr_fact WHERE event_id=? AND field='stated_failure'",
                             (a["id"],)).fetchone()[0]
    assert stated == "QUOTE_VERIFIED"
    out = packs.export_packs(rh, tmp_path / "packs", [a["id"]])
    text = open(out[a["id"]]["path"]).read()
    assert "RETROSPECTIVE AND POTENTIALLY CONTAMINATED" in text and "hiding names does not prevent this" in text
    pid = rh.conn.execute("SELECT passage_id FROM sr_fact WHERE event_id=? AND field='stated_failure'", (a["id"],)).fetchone()[0]
    j = {"event_id": a["id"], "failure_scope": "stated primary endpoint miss in disease X only",
         "remaining_business": "AB-202 Phase 2", "financing": "about 20 months of historical cash use",
         "competing_explanations": "trial design vs. drug inactivity",
         "recovery_thesis": {"possible_catalysts": "AB-202 data 2H 2021", "supporting_evidence": "cash",
                             "invalidating_evidence": "AB-202 failure", "open_questions": "platform read-through"},
         "claims": [{"text": "AB-101 did not meet its primary endpoint in disease X.", "claim_type": "FACT",
                     "citations": [{"passage_id": pid, "quote": "did not meet its primary endpoint"}]},
                    {"text": "The remaining pipeline may carry the company.", "claim_type": "OPINION"}]}
    (tmp_path / "j.json").write_text(json.dumps({"judgments": [j]}))
    res = packs.import_judgments(rh, tmp_path / "j.json")
    assert res["stored"] == [a["id"]] and res["label"] == "RETROSPECTIVE_CONTAMINATED"
    row = rh.conn.execute("SELECT label, verification_json FROM sr_judgment").fetchone()
    assert row[0] == "RETROSPECTIVE_CONTAMINATED"
    assert [v["status"] for v in json.loads(row[1])][1] == "NOT_REQUIRED"              # opinion stays an opinion
    bad = dict(j, financing="a 40% chance of recovery")
    (tmp_path / "bad.json").write_text(json.dumps({"judgments": [bad]}))
    with pytest.raises(Exception, match="probabilities"):
        packs.import_judgments(rh, tmp_path / "bad.json")


def test_pipeline_is_idempotent(rh):
    _ran(rh)
    counts = {t: rh.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("sr_event", "sr_fact", "sr_source", "sr_eligibility", "sr_outcome_audit", "price_bar")}
    pipeline.run(rh, fetch=_fetch, history=_history(), fetch_facts=_facts, today=date(2026, 10, 2))
    assert {t: rh.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts} == counts
