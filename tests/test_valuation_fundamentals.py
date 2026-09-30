"""Valuation (§18.7-9, 12) and point-in-time fundamentals (§18.5, 10)."""

from datetime import date, datetime, timezone
from decimal import Decimal as Dec

import pytest

from equity_monitor.data.securities import get_or_create_issuer
from equity_monitor.evaluation.returns import irr_from_times, time_weighted_return, xirr
from equity_monitor.research.fundamentals import FactView, add_fact
from equity_monitor.valuation.builder import MissingInputs, build_scenarios
from equity_monitor.valuation.dcf import (
    A, ScenarioInputs, Series, Source, ValuationError, judg, margin_of_safety, reverse_dcf, run_dcf,
)
from equity_monitor.config.models import ValuationDefaults


def hand_example(**over) -> ScenarioInputs:
    s = dict(
        base_revenue=judg(1000, "t"), revenue_growth=Series(values=[Dec("0.10"), Dec("0.10")], source=Source(kind="USER")),
        ebit_margin=Series(values=[Dec("0.20"), Dec("0.20")], source=Source(kind="USER")), tax_rate=judg("0.25", "t"),
        dna_pct_revenue=judg("0.05", "t"), capex_pct_revenue=judg("0.06", "t"), nwc_pct_incremental_revenue=judg("0.10", "t"),
        wacc=judg("0.10", "t"), terminal_growth=judg("0.02", "t"), cash_and_investments=judg(100, "t"), debt=judg(300, "t"),
        minority_interest=judg(30, "t"), diluted_shares=judg(100, "t"))
    s.update(over)
    return ScenarioInputs(**s)


def test_dcf_matches_hand_worked_example():  # §18.7
    # Year 1: rev 1100, EBIT 220, NOPAT 165, D&A 55, capex 66, dNWC 10  -> FCFF 144
    # Year 2: rev 1210, EBIT 242, NOPAT 181.5, D&A 60.5, capex 72.6, dNWC 11 -> FCFF 158.4
    # TV_2 = 158.4 * 1.02 / (0.10 - 0.02) = 2019.6
    # EV = 144/1.1 + 158.4/1.21 + 2019.6/1.21 = 130.9091 + 130.9091 + 1669.0909 = 1930.9091
    # Equity = 1930.9091 + 100 - 300 - 30 = 1700.9091 ; per share = 17.009091
    r = run_dcf(hand_example())
    assert [round(x.fcff, 6) for x in r.rows] == [Dec("144"), Dec("158.4")]
    assert round(r.terminal_value, 6) == Dec("2019.6")
    assert round(r.enterprise_value, 4) == Dec("1930.9091")
    assert round(r.value_per_share, 6) == Dec("17.009091")
    assert round(r.terminal_share, 6) == round(Dec("1669.090909") / Dec("1930.909091"), 6)


def test_invalid_terminal_growth_fails():  # §18.8
    with pytest.raises(ValuationError, match="below WACC"):
        run_dcf(hand_example(terminal_growth=judg("0.10", "t")))
    with pytest.raises(ValuationError, match="below WACC"):
        run_dcf(hand_example(terminal_growth=judg("0.12", "t")))
    with pytest.raises(ValuationError, match="policy cap"):
        run_dcf(hand_example(terminal_growth=judg("0.05", "t")), terminal_growth_cap=Dec("0.04"))


def test_debt_cash_dilution_effects():  # §18.9
    base = run_dcf(hand_example())
    more_debt = run_dcf(hand_example(debt=judg(400, "t")))
    more_cash = run_dcf(hand_example(cash_and_investments=judg(200, "t")))
    more_shares = run_dcf(hand_example(diluted_shares=judg(110, "t")))
    assert base.enterprise_value == more_debt.enterprise_value      # debt does not touch EV (no double count)
    assert round(base.equity_value - more_debt.equity_value, 9) == 100
    assert round(more_cash.equity_value - base.equity_value, 9) == 100
    assert round(more_shares.value_per_share, 6) == round(base.equity_value / 110, 6)
    with pytest.raises(ValuationError, match="double count"):
        run_dcf(hand_example(annual_net_dilution=judg("0.02", "t")))
    addback = run_dcf(hand_example(sbc_treatment="add_back", sbc_pct_revenue=judg("0.02", "t"),
                                   annual_net_dilution=judg("0.02", "t")))
    assert addback.shares == Dec(100) * Dec("1.02") ** 2
    assert addback.rows[0].sbc_addback == Dec("22")


def test_margin_of_safety_only_when_meaningful():
    assert margin_of_safety(Dec(75), Dec(100)) == Dec("0.25")
    assert margin_of_safety(Dec(75), Dec(-5)) is None
    assert margin_of_safety(Dec(75), Dec(100), meaningful=False) is None


def test_reverse_dcf_solves_and_reports_unbounded():
    s = hand_example()
    r = reverse_dcf(s, Dec("17.009091"), "wacc")
    assert r.status == "SOLVED" and abs(r.implied_value - Dec("0.10")) < Dec("0.0001")
    r2 = reverse_dcf(s, Dec("10000"), "ebit_margin")
    assert r2.status == "NOT_BOUNDED" and r2.implied_value is None
    r3 = reverse_dcf(s, Dec("12"), "revenue_growth")
    assert r3.status == "SOLVED"
    assert "wacc" in r3.fixed_assumptions and "revenue_growth" not in r3.fixed_assumptions


def test_irr_edge_cases():
    assert irr_from_times([(0, -100), (1, 110)]).status == "SOLVED"
    assert abs(irr_from_times([(0, -100), (1, 110)]).rate - 0.10) < 1e-9
    assert irr_from_times([(0, 100), (1, 110)]).status == "NO_SOLUTION"
    multi = irr_from_times([(0, -100), (1, 230), (2, -132)])   # roots at 10% and 20%
    assert multi.status == "MULTIPLE_SOLUTIONS" and len(multi.roots) == 2
    assert xirr([]).status == "INSUFFICIENT_DATA"


def test_twr_neutral_to_flows():
    navs = [(date(2026, 1, 1), Dec(100)), (date(2026, 1, 2), Dec(1110)), (date(2026, 1, 3), Dec(1221))]
    twr, _ = time_weighted_return(navs, {date(2026, 1, 2): Dec(1000)})   # +1000 deposit at start of day 2
    assert round(twr, 6) == round(Dec(1110) / Dec(1100) * Dec("1.1") - 1, 6)


# ---------------------------------------------------------------- fundamentals
def _t(y, m, d, hh=21):
    return datetime(y, m, d, hh, tzinfo=timezone.utc)


def test_restatement_does_not_leak_into_earlier_as_of(app):  # §18.5
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="Co", cik="1")
    add_fact(app, iss, "revenue", 1000, start=date(2024, 1, 1), end=date(2024, 12, 31), public_at=_t(2025, 2, 20), accession="A1")
    add_fact(app, iss, "revenue", 900, start=date(2024, 1, 1), end=date(2024, 12, 31), public_at=_t(2025, 8, 1), accession="A2-restated")
    before = FactView(app, iss, _t(2025, 7, 31))
    after = FactView(app, iss, _t(2025, 8, 2))
    assert before.annual("revenue")[-1].value == 1000
    assert after.annual("revenue")[-1].value == 900
    assert FactView(app, iss, _t(2025, 2, 19)).annual("revenue") == []


def test_quarterly_from_ytd_and_units(app):  # §18.10
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="Co", cik="2")
    # cash flow statements are year-to-date in 10-Qs
    add_fact(app, iss, "cfo", 100, start=date(2025, 1, 1), end=date(2025, 3, 31), public_at=_t(2025, 5, 1), accession="Q1")
    add_fact(app, iss, "cfo", 250, start=date(2025, 1, 1), end=date(2025, 6, 30), public_at=_t(2025, 8, 1), accession="Q2")
    add_fact(app, iss, "cfo", 420, start=date(2025, 1, 1), end=date(2025, 9, 30), public_at=_t(2025, 11, 1), accession="Q3")
    add_fact(app, iss, "cfo", 600, start=date(2025, 1, 1), end=date(2025, 12, 31), public_at=_t(2026, 2, 20), accession="FY")
    fv = FactView(app, iss, _t(2026, 3, 1))
    qs = fv.quarters("cfo")
    assert [q.value for q in qs] == [100, 150, 170, 180]
    assert [q.derived for q in qs] == [False, True, True, True]
    assert fv.ttm("cfo").value == 600
    # Q4 not derivable before the 10-K is public
    assert len(FactView(app, iss, _t(2026, 2, 19)).quarters("cfo")) == 3
    assert FactView(app, iss, _t(2026, 2, 19)).ttm("cfo") is None
    # the shares concept is stored in 'shares', not USD
    add_fact(app, iss, "shares_outstanding", 1_000_000, start=None, end=date(2025, 12, 31), public_at=_t(2026, 2, 20), accession="FY")
    row = app.conn.execute("SELECT unit FROM financial_fact WHERE concept='shares_outstanding'").fetchone()
    assert row["unit"] == "shares"


def _company(app, cik, margin: Dec):
    iss = get_or_create_issuer(app.conn, app.now_iso(), name=f"Co{cik}", cik=cik)
    rev = [Dec(1000), Dec(1100), Dec(1210), Dec(1331)]
    for i, y in enumerate(range(2022, 2026)):
        s, e, p = date(y, 1, 1), date(y, 12, 31), _t(y + 1, 2, 20)
        add_fact(app, iss, "revenue", rev[i], start=s, end=e, public_at=p, accession=f"{cik}-{y}")
        add_fact(app, iss, "operating_income", rev[i] * margin, start=s, end=e, public_at=p, accession=f"{cik}-{y}")
        add_fact(app, iss, "shares_diluted_weighted", 100, start=s, end=e, public_at=p, accession=f"{cik}-{y}")
        add_fact(app, iss, "cash", 50, start=None, end=e, public_at=p, accession=f"{cik}-{y}")
        add_fact(app, iss, "long_term_debt", 100, start=None, end=e, public_at=p, accession=f"{cik}-{y}")
    return iss


def test_weaker_fundamentals_reduce_value_with_price_unchanged(app):  # §18.12
    strong = build_scenarios(FactView(app, _company(app, "10", Dec("0.25")), _t(2026, 6, 1)), ValuationDefaults())
    weak = build_scenarios(FactView(app, _company(app, "11", Dec("0.15")), _t(2026, 6, 1)), ValuationDefaults())
    assert run_dcf(weak["base"]).value_per_share < run_dcf(strong["base"]).value_per_share
    for sc in ("bear", "base", "bull"):
        assert run_dcf(strong[sc]).value_per_share > 0
    assert run_dcf(strong["bear"]).value_per_share < run_dcf(strong["base"]).value_per_share < run_dcf(strong["bull"]).value_per_share
    # every assumption carries a source
    for k, v in strong["base"].model_dump().items():
        if isinstance(v, dict) and "source" in v:
            assert v["source"]["kind"] in ("FACT", "DERIVED", "ANALYST_JUDGMENT", "POLICY_DEFAULT", "USER")


def test_missing_inputs_block_valuation(app):
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="Empty", cik="12")
    with pytest.raises(MissingInputs) as e:
        build_scenarios(FactView(app, iss, _t(2026, 6, 1)), ValuationDefaults())
    assert "revenue" in str(e.value)
