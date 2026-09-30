"""Benchmarks, performance and paper execution (§18.25, paper rules)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as Dec

import pytest

from equity_monitor.config.models import Policy
from equity_monitor.data.prices import Action, Bar, PriceFetch, store_fetch, total_return_index
from equity_monitor.data.securities import register_security
from equity_monitor.decisions.recommend import generate
from equity_monitor.evaluation.paper import PaperError, fill_session, paper_execute
from equity_monitor.evaluation.performance import contribution_matched, performance, process_metrics
from equity_monitor.fixtures import AS_OF, build_demo
from equity_monitor.ledger.csv_import import import_csv
from equity_monitor.ledger.store import create_account, create_portfolio


@pytest.fixture
def bench_setup(app):
    sid = register_security(app.conn, app.now_iso(), "BM", security_type="ETF")
    bars = [Bar(date(2026, 3, 2), Dec(100)), Bar(date(2026, 3, 3), Dec(100)), Bar(date(2026, 3, 4), Dec(50)),
            Bar(date(2026, 3, 5), Dec(50)), Bar(date(2026, 3, 6), Dec(55))]
    acts = [Action("SPLIT", date(2026, 3, 4), Dec(2), Dec(1)), Action("CASH_DIVIDEND", date(2026, 3, 5), cash_amount=Dec(1))]
    store_fetch(app, sid, PriceFetch(bars, acts), "fixture")
    pf = create_portfolio(app, "cashonly", "ACTUAL")
    a = create_account(app, pf, "a")
    import_csv(app, a, text="date,type,amount\n2026-03-01,DEPOSIT,1000\n2026-03-04,DEPOSIT,500\n2026-03-06,WITHDRAWAL,300\n")
    return pf, sid


def test_contribution_matched_benchmark_hand_computed(app, bench_setup):  # §18.25
    pf, _ = bench_setup
    b = contribution_matched(app, pf, "BM", date(2026, 3, 1), date(2026, 3, 6))
    vals = dict(b.values)
    # 03-02: 1000/100 = 10 units. 03-04: 2:1 split -> 20 units, +500/50 -> 30. 03-05: dividend 1.0 reinvested
    # at 50 -> +0.6 -> 30.6 units (1530). 03-06: withdraw 300 at 55 -> 30.6 - 5.4545... units.
    assert vals[date(2026, 3, 2)] == Dec(1000)
    assert vals[date(2026, 3, 4)] == Dec(1500)
    assert vals[date(2026, 3, 5)] == Dec(1530)
    assert round(vals[date(2026, 3, 6)], 6) == Dec("1383.000000")
    assert not b.unapplied


def test_total_return_index_no_double_counting(app, bench_setup):
    _, sid = bench_setup
    tri = total_return_index(app, sid, date(2026, 3, 2), date(2026, 3, 6))
    # 1 unit -> split to 2 -> dividend adds 2*1/50 = 0.04 -> 2.04 units * 55
    assert tri[date(2026, 3, 4)] == Dec(100)
    assert tri[date(2026, 3, 6)] == Dec("2.04") * 55


def test_performance_report_cash_portfolio(app, bench_setup):
    pf, _ = bench_setup
    rep = performance(app, pf, date(2026, 3, 2), date(2026, 3, 6), benchmarks=["BM"])
    assert rep["twr"] == 0                        # all cash: flows do not create return
    assert rep["pre_tax"] and rep["label"] == "ACTUAL"
    assert rep["mwr"]["status"] in ("SOLVED", "NO_SOLUTION")
    assert rep["benchmarks"]["BM"]["end_value"] is not None
    assert rep["nav_reconciliation"]["difference_is_standalone_fees"] == 0


def test_paper_fill_uses_next_open_never_earlier_price(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    rec = generate(app, d["portfolio_id"], d["securities"]["ZZADD"]["security_id"])
    assert fill_session("2026-09-30T22:00:00.000000Z") == date(2026, 10, 1)
    assert fill_session("2026-10-01T13:00:00.000000Z") == date(2026, 10, 1)      # before the 09:30 ET open
    assert fill_session("2026-10-01T13:31:00.000000Z") == date(2026, 10, 2)      # after the open -> next session
    paper = create_portfolio(app, "shadow", "PAPER")
    pa = create_account(app, paper, "paper")
    import_csv(app, pa, text="date,type,amount\n2026-09-01,DEPOSIT,10000\n")
    with pytest.raises(PaperError):
        paper_execute(app, rec, paper)                                            # policy not frozen
    app.policy = Policy(status="FROZEN")
    with pytest.raises(PaperError):
        paper_execute(app, rec, d["portfolio_id"])                                # not a PAPER portfolio
    assert paper_execute(app, rec, paper) is None                                 # Oct 1 bar not yet available
    sid = d["securities"]["ZZADD"]["security_id"]
    store_fetch(app, sid, PriceFetch([Bar(date(2026, 10, 1), Dec("7.00"), open=Dec("6.00"))]), "fixture")
    pid = paper_execute(app, rec, paper)
    row = app.conn.execute("SELECT * FROM paper_execution WHERE id=?", (pid,)).fetchone()
    assert row["fill_session_date"] == "2026-10-01" and Dec(row["fill_price"]) == Dec("6.00") * Dec("1.0005")
    assert paper_execute(app, rec, paper) is None                                 # at most once


def test_process_metrics_runs(app):
    m = process_metrics(app)
    assert m["recommendations"] == 0 and m["unsupported_claim_rate"] is None
