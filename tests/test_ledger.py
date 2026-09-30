"""Accounting acceptance tests (spec §18 items 1-4, 20) with hand-computed expectations."""

from datetime import date
from decimal import Decimal as Dec

import pytest

from equity_monitor.data.prices import Action, Bar, PriceFetch, store_fetch
from equity_monitor.data.securities import find_security
from equity_monitor.ledger.csv_import import import_csv
from equity_monitor.ledger.reconcile import check_provider_actions, import_snapshot, reconcile_snapshot
from equity_monitor.ledger.replay import NegativeHoldingError
from equity_monitor.ledger.store import (
    NewEvent, PortfolioMixError, assert_same_kind, correct_event, create_account, create_portfolio,
    load_events, open_issues, record_events,
)
from equity_monitor.ledger.views import portfolio_view

LEDGER_CSV = """date,type,symbol,quantity,price,fees,amount,split_from,split_to,external_id,note
2026-01-05,DEPOSIT,,,,,10000,,,,initial
2026-01-06,BUY,AAA,100,50,1,,,,,
2026-02-10,DIVIDEND,AAA,,,,50,,,,
2026-03-02,DIVIDEND_REINVEST,AAA,0.5,50,0,25,,,,drip
2026-04-01,SPLIT,AAA,,,,,1,2,,2-for-1
2026-05-04,SELL,AAA,150,30,1,,,,,
2026-05-05,FEE,,,,,5,,,,account fee
2026-06-01,WITHDRAWAL,,,,,1000,,,,
"""


@pytest.fixture
def acct(app):
    pf = create_portfolio(app, "main", "ACTUAL")
    return pf, create_account(app, pf, "taxable-1")


def _price(app, symbol, d, close):
    sid = find_security(app.conn, symbol)
    store_fetch(app, sid, PriceFetch([Bar(d, Dec(close))]), "fixture")


def test_reimport_same_csv_creates_no_duplicates(app, acct):  # §18.1
    _, a = acct
    r1 = import_csv(app, a, text=LEDGER_CSV)
    assert r1.inserted == 9 and not r1.rejected       # DRIP row expands to dividend + buy
    r2 = import_csv(app, a, text=LEDGER_CSV)
    assert r2.inserted == 0 and r2.duplicates == 9 and r2.already_imported_file
    assert len(load_events(app, a)) == 9


def test_identical_rows_within_one_file_are_distinct_events(app, acct):
    _, a = acct
    text = "date,type,symbol,quantity,price,amount\n2026-01-05,DEPOSIT,,,,1000\n" \
           "2026-01-06,BUY,BBB,1,10,\n2026-01-06,BUY,BBB,1,10,\n"
    assert import_csv(app, a, text=text).inserted == 3
    assert import_csv(app, a, text=text).inserted == 0


def test_full_cycle_reconciles(app, acct):  # §18.2
    pf, a = acct
    import_csv(app, a, text=LEDGER_CSV)
    _price(app, "AAA", date(2026, 6, 1), "31")

    v = portfolio_view(app, pf, as_of=date(2026, 5, 4))
    assert v.cash == Dec("9548")                    # 10000-5001+50+25-25+4499
    assert v.unsettled_cash == Dec("4499")          # T+1 settlement on 2026-05-05
    assert v.available_cash == Dec("5049")

    v = portfolio_view(app, pf, as_of=date(2026, 6, 1))
    h = v.holding(find_security(app.conn, "AAA"))
    assert h.shares == Dec("51")                    # (100 + 0.5) * 2 - 150
    assert h.cost_basis == Dec("1275.25")           # 5001*50/200 + 25
    assert h.market_value == Dec("1581")
    assert h.unrealized_gain == Dec("305.75")
    assert v.realized_gain == Dec("748.25")         # 4499 - 5001*150/200
    assert v.dividends == Dec("75")
    assert v.fees == Dec("7")
    assert v.cash == Dec("8543")
    assert v.nav == Dec("10124")
    # NAV reconciliation: net contributions + P&L components
    net_contrib = Dec("9000")
    assert v.nav - net_contrib == v.realized_gain + h.unrealized_gain + v.dividends - Dec("5")


def test_unknown_cost_basis_is_not_zero(app, acct):  # §18.3
    pf, a = acct
    text = ("date,type,symbol,quantity,cost_basis,price,amount\n"
            "2026-01-02,OPENING_POSITION,CCC,10,,,\n"
            "2026-01-02,OPENING_POSITION,DDD,10,500,,\n"
            "2026-01-02,OPENING_CASH,,,,,100\n"
            "2026-03-02,SELL,CCC,4,,60,\n")
    assert not import_csv(app, a, text=text).rejected
    _price(app, "CCC", date(2026, 3, 2), "60")
    _price(app, "DDD", date(2026, 3, 2), "70")
    v = portfolio_view(app, pf, as_of=date(2026, 3, 2))
    ccc = v.holding(find_security(app.conn, "CCC"))
    assert ccc.cost_basis is None and ccc.unrealized_gain is None
    assert ccc.unknown_basis_shares == Dec("6")
    assert ccc.market_value == Dec("360")           # market value still tracked
    assert v.realized_gain is None and v.realized_unknown_lots == 1
    ddd = v.holding(find_security(app.conn, "DDD"))
    assert ddd.unrealized_gain == Dec("200")
    assert v.nav == Dec("100") + Dec("240") + Dec("360") + Dec("700")


def test_negative_holdings_rejected(app, acct):
    _, a = acct
    rep = import_csv(app, a, text="date,type,symbol,quantity,price,amount\n2026-01-05,DEPOSIT,,,,100\n"
                                  "2026-01-06,SELL,EEE,1,10,\n")
    assert rep.inserted == 1 and len(rep.rejected) == 1 and "long-only" in rep.rejected[0]["error"]
    with pytest.raises(NegativeHoldingError):
        from equity_monitor.ledger.replay import replay_account, LedgerEvent
        replay_account(a, [LedgerEvent("x", 1, a, "SELL", date(2026, 1, 1), "s", quantity=Dec(1), price=Dec(1))])


def test_backdated_sale_that_breaks_history_is_rejected(app, acct):
    _, a = acct
    import_csv(app, a, text="date,type,symbol,quantity,price,amount\n2026-01-05,DEPOSIT,,,,1000\n"
                            "2026-01-06,BUY,FFF,10,10,\n2026-02-06,SELL,FFF,10,12,\n")
    sid = find_security(app.conn, "FFF")
    res = record_events(app, a, [NewEvent("SELL", date(2026, 1, 20), sid, quantity=Dec(5), price=Dec(11))])
    assert res.rejected and not res.inserted


def test_unsupported_corporate_action_creates_issue(app, acct):  # §18.4
    pf, a = acct
    import_csv(app, a, text="date,type,symbol,quantity,price,amount,subtype\n2026-01-05,DEPOSIT,,,,1000,\n"
                            "2026-01-06,BUY,GGG,10,10,,\n2026-04-01,CORPORATE_ACTION,GGG,,,,SPINOFF\n")
    issues = open_issues(app, [a])
    assert [i["issue_type"] for i in issues] == ["UNSUPPORTED_CORPORATE_ACTION"]
    assert issues[0]["severity"] == "CRITICAL"
    v = portfolio_view(app, pf, as_of=date(2026, 4, 2))
    assert v.holding(find_security(app.conn, "GGG")).frozen_reason.startswith("SPINOFF")


def test_provider_split_without_ledger_event_is_flagged(app, acct):
    _, a = acct
    import_csv(app, a, text="date,type,symbol,quantity,price,amount\n2026-01-05,DEPOSIT,,,,1000\n"
                            "2026-01-06,BUY,HHH,10,10,\n")
    sid = find_security(app.conn, "HHH")
    store_fetch(app, sid, PriceFetch([], [Action("SPLIT", date(2026, 6, 1), Dec(4), Dec(1))]), "fixture")
    found = check_provider_actions(app, a, date(2026, 9, 30))
    assert found[0]["type"] == "MISSING_SPLIT_EVENT"


def test_snapshot_discrepancy_recorded_not_overwritten(app, acct):
    pf, a = acct
    import_csv(app, a, text=LEDGER_CSV)
    snap = import_snapshot(app, a, date(2026, 6, 1), text="type,symbol,quantity,cash\nPOSITION,AAA,52,\nCASH,,,8543\n")
    found = reconcile_snapshot(app, snap)
    assert [f["type"] for f in found] == ["SNAPSHOT_QUANTITY_MISMATCH"]
    assert portfolio_view(app, pf, as_of=date(2026, 6, 1)).holding(find_security(app.conn, "AAA")).shares == Dec(51)


def test_correction_is_append_only(app, acct):
    pf, a = acct
    import_csv(app, a, text="date,type,symbol,quantity,price,amount\n2026-01-05,DEPOSIT,,,,1000\n"
                            "2026-01-06,BUY,JJJ,10,10,\n")
    buy = [e for e in load_events(app, a) if e.event_type == "BUY"][0]
    correct_event(app, buy.id, NewEvent("BUY", date(2026, 1, 6), buy.security_id, quantity=Dec(10), price=Dec(11)),
                  "wrong price")
    evs = load_events(app, a)
    assert len(evs) == 4  # deposit, original buy, reversal, replacement
    v = portfolio_view(app, pf, as_of=date(2026, 1, 6))
    assert v.cash == Dec(890)
    with pytest.raises(Exception):
        app.conn.execute("UPDATE ledger_event SET price='1' WHERE id=?", (buy.id,))
    with pytest.raises(Exception):
        app.conn.execute("DELETE FROM ledger_event WHERE id=?", (buy.id,))


def test_actual_paper_fixture_cannot_mix(app):  # §18.20
    actual = create_portfolio(app, "real", "ACTUAL")
    paper = create_portfolio(app, "shadow", "PAPER")
    fixture = create_portfolio(app, "demo", "FIXTURE")
    with pytest.raises(PortfolioMixError):
        assert_same_kind(app, [actual, paper])
    with pytest.raises(PortfolioMixError):
        assert_same_kind(app, [actual, fixture])
    with pytest.raises(PortfolioMixError):
        create_portfolio(app, "real", "PAPER")
    a1 = create_account(app, actual, "x")
    a2 = create_account(app, paper, "x")
    import_csv(app, a1, text="date,type,amount\n2026-01-05,DEPOSIT,100\n")
    import_csv(app, a2, text="date,type,amount\n2026-01-05,DEPOSIT,999\n")
    assert portfolio_view(app, actual, as_of=date(2026, 1, 6)).cash == Dec(100)
    assert portfolio_view(app, paper, as_of=date(2026, 1, 6)).kind == "PAPER"


def test_documented_example_files_import_cleanly(app, acct):
    from pathlib import Path
    pf, a = acct
    root = Path(__file__).resolve().parents[1]
    rep = import_csv(app, a, root / "examples" / "transactions.example.csv")
    assert not rep.rejected and rep.inserted == 8
    snap = import_snapshot(app, a, date(2026, 7, 16), root / "examples" / "snapshot.example.csv")
    found = reconcile_snapshot(app, snap)
    assert found == []
