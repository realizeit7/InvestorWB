"""Regression tests for the correctness-repair milestone (review of a2d0ef8). Each test fails on the defective
behaviour it names."""

from datetime import date, datetime, timezone
from decimal import Decimal as Dec

import pytest

from equity_monitor.config.models import ValuationDefaults
from equity_monitor.data.securities import get_or_create_issuer
from equity_monitor.research.fundamentals import FactView, add_fact, debt_total
from equity_monitor.valuation.builder import build_scenarios


def _t(y, m, d, hh=21):
    return datetime(y, m, d, hh, tzinfo=timezone.utc)


# ------------------------------------------------------------------ #5 debt normalization
BS = date(2025, 12, 31)
PUB = _t(2026, 2, 20)


def _bs(app, cik, facts: dict, end: date = BS, acc: str | None = None):
    iss = get_or_create_issuer(app.conn, app.now_iso(), name=f"Debt{cik}", cik=cik)
    for concept, v in facts.items():
        e = v[1] if isinstance(v, tuple) else end
        val = v[0] if isinstance(v, tuple) else v
        add_fact(app, iss, concept, val, start=None, end=e, public_at=PUB, accession=acc or f"{cik}-{e}")
    return iss


def _operating(app, iss, cik):
    for y in range(2022, 2026):
        s, e, p = date(y, 1, 1), date(y, 12, 31), _t(y + 1, 2, 20)
        for c, v in (("revenue", 1000), ("operating_income", 200), ("dna", 50), ("shares_diluted_weighted", 100)):
            add_fact(app, iss, c, v, start=s, end=e, public_at=p, accession=f"{cik}-{y}")


def test_total_and_component_are_not_double_counted(app):
    # DebtCurrent (300) already contains current maturities of LTD (100): total current debt is 300, not 400
    iss = _bs(app, "901", {"long_term_debt_noncurrent": 800, "long_term_debt_current": 100, "debt_current": 300})
    agg = debt_total(FactView(app, iss, _t(2026, 3, 1)))
    assert agg.value == Dec(1100) and agg.complete and agg.basis == "DERIVED"
    # LongTermDebt (total incl. current maturities) + LongTermDebtCurrent: current maturities are not added twice
    iss2 = _bs(app, "902", {"long_term_debt_total": 900, "long_term_debt_current": 100, "short_term_borrowings": 50})
    agg2 = debt_total(FactView(app, iss2, _t(2026, 3, 1)))
    assert agg2.value == Dec(950) and agg2.complete
    # LongTermDebt alone already includes current maturities; only short-term borrowings are added
    iss3 = _bs(app, "903", {"long_term_debt_total": 900, "short_term_borrowings": 50})
    assert debt_total(FactView(app, iss3, _t(2026, 3, 1))).value == Dec(950)


def test_separate_short_term_borrowings_and_current_maturities_are_both_counted(app):
    # the old normalizer took only the first current-debt tag (current maturities) and silently dropped STB
    iss = _bs(app, "904", {"long_term_debt_noncurrent": 800, "long_term_debt_current": 100, "short_term_borrowings": 250})
    agg = debt_total(FactView(app, iss, _t(2026, 3, 1)))
    assert agg.value == Dec(1150) and agg.complete
    assert {c[0] for c in agg.components} == {"long_term_debt_noncurrent", "long_term_debt_current", "short_term_borrowings"}
    # commercial paper is not added on top of short-term borrowings (commonly included) and says so
    iss2 = _bs(app, "905", {"long_term_debt_noncurrent": 800, "long_term_debt_current": 100,
                            "short_term_borrowings": 250, "commercial_paper": 200})
    agg2 = debt_total(FactView(app, iss2, _t(2026, 3, 1)))
    assert agg2.value == Dec(1150) and any("commercial paper" in w for w in agg2.warnings)


def test_missing_debt_components_stay_unknown(app):
    # only noncurrent LTD is reported: current debt is unknown, not zero
    iss = _bs(app, "906", {"long_term_debt_noncurrent": 800, "cash": 100})
    agg = debt_total(FactView(app, iss, _t(2026, 3, 1)))
    assert not agg.complete and agg.basis is None
    assert "current maturities of long-term debt" in agg.missing and "short-term borrowings" in agg.missing
    _operating(app, iss, "906")
    sc = build_scenarios(FactView(app, iss, _t(2026, 3, 1)), ValuationDefaults())["base"]
    assert sc.debt.source.kind == "ANALYST_JUDGMENT"                    # never labelled FACT with assumptions inside
    assert "assumed 0" in sc.debt.source.note
    assert any("debt: components not reported" in f for f in sc.review_flags)
    # no debt concept at all: nothing is invented
    iss2 = _bs(app, "907", {"cash": 100})
    agg2 = debt_total(FactView(app, iss2, _t(2026, 3, 1)))
    assert agg2.value is None and not agg2.complete


def test_debt_components_from_different_dates_are_not_combined(app):
    iss = _bs(app, "908", {"long_term_debt_noncurrent": 800, "debt_current": (500, date(2025, 9, 30))})
    agg = debt_total(FactView(app, iss, _t(2026, 3, 1)))
    assert agg.period_end == BS and agg.value == Dec(800)
    assert not agg.complete                                             # current debt at 2025-12-31 is unknown
    assert any("other dates" in w for w in agg.warnings)


def test_screening_treats_incomplete_debt_as_unknown(app):
    from equity_monitor.research.screening import ScreenInput, score
    rows = []
    for i, complete in enumerate((True, False)):
        cik = f"91{i}"
        facts = {"long_term_debt_noncurrent": 800, "cash": 100, "short_term_investments": 0}
        if complete:
            facts["debt_current"] = 0
        iss = _bs(app, cik, facts)
        _operating(app, iss, cik)
        rows.append(ScreenInput(f"s{i}", f"S{i}", "3570", "SIC35", FactView(app, iss, _t(2026, 3, 1)), Dec(20)))
    res = {r.symbol: r for r in score(app, rows)}
    assert res["S0"].metrics["net_debt_to_ebitda"] is not None
    assert res["S1"].metrics["net_debt_to_ebitda"] is None and res["S1"].metrics.get("roic") is None
    assert "incomplete" in res["S1"].notes["debt"]


def test_valuation_with_assumptions_needs_explicit_acceptance(app):
    from equity_monitor.data.securities import register_security
    from equity_monitor.valuation.store import AssumptionsNotAcknowledged, approve_valuation, create_valuation
    iss = _bs(app, "920", {"long_term_debt_noncurrent": 800, "cash": 100})
    _operating(app, iss, "920")
    sid = register_security(app.conn, app.now_iso(), "DEBTX", issuer_id=iss)
    sc = build_scenarios(FactView(app, iss, _t(2026, 3, 1)), ValuationDefaults())
    vid = create_valuation(app, sid, sc, evidence_as_of=_t(2026, 3, 1))
    with pytest.raises(AssumptionsNotAcknowledged, match="ASSUMED 0"):
        approve_valuation(app, vid, downside_reviewed=True)
    approve_valuation(app, vid, downside_reviewed=True, accept_assumptions=True)
    note = app.conn.execute("SELECT note FROM valuation_approval WHERE valuation_version_id=?", (vid,)).fetchone()["note"]
    assert "accepted assumptions" in note and "debt" in note


def test_legacy_debt_concepts_are_remapped_by_source_tag(tmp_path):
    import sqlite3
    from equity_monitor.db.core import _migration_files, _split_sql
    conn = sqlite3.connect(":memory:")
    for name, sql in _migration_files():
        if name.startswith("0003"):
            break
        for stmt in _split_sql(sql):
            conn.execute(stmt)
    conn.execute("INSERT INTO issuer(id, name, created_at) VALUES ('i', 'x', 't')")
    for i, (concept, tag) in enumerate((("long_term_debt", "LongTermDebt"), ("long_term_debt", "LongTermDebtNoncurrent"),
                                        ("current_debt", "LongTermDebtCurrent"), ("current_debt", "DebtCurrent"),
                                        ("current_debt", "ShortTermBorrowings"))):
        conn.execute("INSERT INTO financial_fact(id, issuer_id, concept, source_tag, unit, value, period_type, period_end,"
                     " accession_no, public_at, normalizer_version, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"f{i}", "i", concept, tag, "USD", "1", "INSTANT", "2025-12-31", f"a{i}", "t", "norm-1", "t"))
    for name, sql in _migration_files():
        if name.startswith("0003"):
            for stmt in _split_sql(sql):
                conn.execute(stmt)
    got = dict(conn.execute("SELECT source_tag, concept FROM financial_fact").fetchall())
    assert got == {"LongTermDebt": "long_term_debt_total", "LongTermDebtNoncurrent": "long_term_debt_noncurrent",
                   "LongTermDebtCurrent": "long_term_debt_current", "DebtCurrent": "debt_current",
                   "ShortTermBorrowings": "short_term_borrowings"}
