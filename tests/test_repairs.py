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


# ------------------------------------------------------------------ #1 citation integrity vs substantive support
from equity_monitor.data.sec import store_passages
from equity_monitor.db.core import insert
from equity_monitor.fixtures import AS_OF, build_demo
from equity_monitor.research.evidence import Citation, ClaimIn, verify_claim, with_llm_assessment

SRC = ("Net revenue increased 12% to $4.2 billion in fiscal 2025 from $3.75 billion in fiscal 2024. "
       "Operating income was $610 million. Net loss from discontinued operations was $(40) million. "
       "The company has no material debt.")


def _doc(app, issuer_id, text, doc_id="doc_r1", fpe="2025-12-31", public_at="2026-02-20T21:00:00.000000Z"):
    insert(app.conn, "source_document", {"id": doc_id, "provider": "FIXTURE", "doc_type": "10-K", "issuer_id": issuer_id,
                                         "accession_no": doc_id, "source_url": None, "title": "t", "fiscal_period_end": fpe,
                                         "filed_date": public_at[:10], "public_at": public_at, "public_at_basis": "PROVIDED",
                                         "retrieved_at": public_at, "raw_object_id": None, "content_hash": None,
                                         "parser_version": None, "trust": "FIXTURE", "items": None, "limitations": None})
    store_passages(app, doc_id, text)
    return f"{doc_id}#p0"


def _v(app, iss, text, quote, pid):
    return verify_claim(app, ClaimIn(text=text, claim_type="FACT", citations=[Citation(passage_id=pid, quote=quote)]),
                        iss, AS_OF)


@pytest.fixture
def cited(app):
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="EvCo", cik="930")
    return iss, _doc(app, iss, SRC)


def test_confirmed_numeric_claim_is_verified_on_every_field(app, cited):
    iss, pid = cited
    v = _v(app, iss, "Revenue grew 12% to $4.2 billion in fiscal 2025", "increased 12% to $4.2 billion", pid)
    assert v.status == "VERIFIED" and v.citation_status == "SOURCE_MATCHED" and v.support_status == "CONFIRMED"
    # the quote is judged in its full sentence: the period comes from the sentence, not the fragment
    assert _v(app, iss, "Revenue was $3.75 billion in 2024", "from $3.75 billion", pid).status == "VERIFIED"


@pytest.mark.parametrize("claim,why", [
    ("Revenue decreased 12% to $4.2 billion in fiscal 2025", "direction"),                # contradiction
    ("Revenue grew 12% to $4.2 trillion in fiscal 2025", "value/scale"),                   # scale mismatch
    ("Revenue grew 12% to $4.2 million in fiscal 2025", "value/scale"),
    ("Revenue grew 12% to $4.2 billion in fiscal 2024", "period"),                         # wrong period
    ("Revenue grew 30% to $4.2 billion", "value/scale"),                                   # wrong number
])
def test_contradicted_claims_fail(app, cited, claim, why):
    iss, pid = cited
    v = _v(app, iss, claim, "increased 12% to $4.2 billion", pid)
    assert v.status == "FAILED" and v.citation_status == "SOURCE_MATCHED" and v.support_status == "CONTRADICTED"
    assert any(why in d for d in v.details), v.details


def test_sign_change_fails(app, cited):
    iss, pid = cited
    v = _v(app, iss, "Net income from discontinued operations was $40 million", "was $(40) million", pid)
    assert v.status == "FAILED" and any("sign" in d for d in v.details), v.details
    assert _v(app, iss, "Net loss was $40 million", "was $(40) million", pid).status == "VERIFIED"


def test_unrelated_and_free_text_claims_are_never_verified(app, cited):
    iss, pid = cited
    unrelated = _v(app, iss, "The company is insolvent.", SRC, pid)
    assert unrelated.status == "SOURCE_MATCHED" and unrelated.support_status == "NOT_CHECKABLE"
    # a valid citation plus a confirmed number does not verify the free-text causal part of a claim
    mixed = _v(app, iss, "Revenue grew 12% because customers switched from a competitor", "increased 12%", pid)
    assert mixed.status == "SOURCE_MATCHED" and any("free-text" in d for d in mixed.details)
    # a number that appears in the source but under another metric is not confirmation
    other_metric = _v(app, iss, "Operating income grew 12%", "increased 12%", pid)
    assert other_metric.status != "VERIFIED"
    # the old digit-substring shortcut is gone: '4' is not supported by '$4.2 billion'
    assert _v(app, iss, "Revenue was $4 billion", "increased 12% to $4.2 billion", pid).status == "FAILED"


def test_fact_citations_check_metric_and_period(app):
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="FactCo", cik="931")
    add_fact(app, iss, "revenue", 1_000_000_000, start=date(2025, 1, 1), end=date(2025, 12, 31), public_at=PUB,
             accession="f-2025", fiscal_year=2025, fiscal_period="FY")
    fid = app.conn.execute("SELECT id FROM financial_fact WHERE issuer_id=?", (iss,)).fetchone()["id"]

    def fv(text):
        return verify_claim(app, ClaimIn(text=text, claim_type="FACT", citations=[Citation(fact_id=fid)]), iss, AS_OF)
    assert fv("Revenue was $1.0 billion in fiscal 2025").status == "VERIFIED"
    assert fv("Revenue was $1.0 billion in fiscal 2023").status == "FAILED"          # wrong period
    assert fv("Revenue was $1.0 million").status == "FAILED"                          # scale
    assert fv("Operating income was $1.0 billion").status != "VERIFIED"              # wrong metric


def test_llm_support_opinion_never_upgrades(app, cited):
    iss, pid = cited
    v = _v(app, iss, "The company is insolvent.", SRC, pid)
    assert with_llm_assessment(v, True).status == "SOURCE_MATCHED"
    ok = _v(app, iss, "Revenue grew 12% to $4.2 billion in fiscal 2025", "increased 12% to $4.2 billion", pid)
    assert with_llm_assessment(ok, False).status == "SOURCE_MATCHED"


def _thesis_with(app, sid, claims):
    from equity_monitor.research.thesis import ThesisContent, create_version, current_version
    base = current_version(app, sid).content
    content = ThesisContent.model_validate({**base, "evidence": [c.model_dump() for c in claims]})
    return create_version(app, sid, content, change_reason="repair test", as_of=AS_OF)


def test_thesis_approval_respects_verification(app):
    from equity_monitor.research.thesis import ThesisEvidenceError, approve_version
    app.clock.set(AS_OF)
    d = build_demo(app)
    sid, iss = d["securities"]["ZZHLD"]["security_id"], d["securities"]["ZZHLD"]["issuer_id"]
    pid = _doc(app, iss, SRC, doc_id="doc_zz", public_at="2026-08-01T21:00:00.000000Z")
    bad = _thesis_with(app, sid, [ClaimIn(text="Revenue decreased 12%", claim_type="FACT",
                                          citations=[Citation(passage_id=pid, quote="increased 12%")])])
    with pytest.raises(ThesisEvidenceError, match="failed verification"):
        approve_version(app, bad, acknowledge_unverified=True)
    weak = _thesis_with(app, sid, [ClaimIn(text="The company has no material debt", claim_type="FACT",
                                           citations=[Citation(passage_id=pid, quote="no material debt")])])
    with pytest.raises(ThesisEvidenceError, match="not substantively verified"):
        approve_version(app, weak)
    approve_version(app, weak, acknowledge_unverified=True)
    note = app.conn.execute("SELECT note FROM thesis_approval WHERE thesis_version_id=?", (weak,)).fetchone()["note"]
    assert "SOURCE_MATCHED" in note


def test_approved_thesis_with_failed_claim_is_review(app):
    from equity_monitor.decisions.recommend import generate, get
    app.clock.set(AS_OF)
    d = build_demo(app)
    sid, iss = d["securities"]["ZZADD"]["security_id"], d["securities"]["ZZADD"]["issuer_id"]
    assert get(app, generate(app, d["portfolio_id"], sid))["action"] == "ADD"
    pid = _doc(app, iss, SRC, doc_id="doc_zz", public_at="2026-08-01T21:00:00.000000Z")
    bad = _thesis_with(app, sid, [ClaimIn(text="Revenue decreased 12%", claim_type="FACT",
                                          citations=[Citation(passage_id=pid, quote="increased 12%")])])
    # an approval recorded before this gate existed (legacy data) must not let a contradicted claim act
    insert(app.conn, "thesis_approval", {"id": "tap_legacy", "thesis_version_id": bad, "approved_at": app.now_iso(),
                                         "approver": "owner", "note": "legacy"})
    r = get(app, generate(app, d["portfolio_id"], sid))
    assert r["action"] == "REVIEW" and "THESIS_EVIDENCE_FAILED" in r["reason_codes"]


def test_legacy_verified_claims_are_downgraded_to_source_matched():
    import sqlite3
    from equity_monitor.db.core import _migration_files, _split_sql
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    files = _migration_files()
    for name, sql in files:
        if name.startswith("0004"):
            break
        for stmt in _split_sql(sql):
            conn.execute(stmt)
    conn.execute("INSERT INTO claim VALUES ('c1','THESIS_VERSION','v','t','FACT','VERIFIED',NULL,'x')")
    conn.execute("INSERT INTO evidence_link(id, claim_id, quote, supports, verified) VALUES ('e1','c1','q',1,1)")
    from equity_monitor.db.core import migrate
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.executemany("INSERT INTO schema_migration VALUES (?, 'x')", [(n,) for n, _ in files if n < "0004"])
    conn.row_factory = sqlite3.Row
    migrate(conn)
    row = conn.execute("SELECT verification, citation_status, support_status FROM claim WHERE id='c1'").fetchone()
    assert tuple(row) == ("SOURCE_MATCHED", "SOURCE_MATCHED", "LEGACY")
    assert conn.execute("SELECT verified FROM evidence_link WHERE id='e1'").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert [r[2] for r in conn.execute("PRAGMA foreign_key_list(evidence_link)") if r[3] == "claim_id"] == ["claim"]


# ------------------------------------------------------------------ #2 issuer aggregation / #3 revalidation at cutoff
from datetime import timedelta

from equity_monitor.config.models import MarketPolicy, Policy, PortfolioPolicy
from equity_monitor.decisions.allocation import propose
from equity_monitor.decisions.recommend import generate, get, review_portfolio, set_watchlist
from equity_monitor.ledger.views import portfolio_view


def _add_share_class(app, d, approve_profile_b=True):
    """ZZADD.B: a second share class of ZZADD's issuer with the same thesis, valuation and prices."""
    from equity_monitor.data.prices import Bar, PriceFetch, store_fetch
    from equity_monitor.data.securities import register_security
    from equity_monitor.market.exposures import approve_profile, create_profile, current_profile
    from equity_monitor.research.thesis import ThesisContent, approve_version, create_version, current_version
    from equity_monitor.valuation.dcf import ScenarioInputs
    from equity_monitor.valuation.store import approve_valuation, create_valuation, latest_valuation
    a = d["securities"]["ZZADD"]
    b = register_security(app.conn, app.now_iso(), "ZZADD.B", security_type="COMMON", issuer_id=a["issuer_id"])
    bars = app.conn.execute("SELECT session_date, close FROM price_bar WHERE security_id=?", (a["security_id"],)).fetchall()
    store_fetch(app, b, PriceFetch([Bar(date.fromisoformat(r["session_date"]), Dec(r["close"])) for r in bars]), "fixture")
    tv = current_version(app, a["security_id"])
    approve_version(app, create_version(app, b, ThesisContent.model_validate(tv.content), change_reason="class B",
                                        author="FIXTURE", label="FIXTURE", as_of=AS_OF))
    val = latest_valuation(app, a["security_id"])
    vid = create_valuation(app, b, {k: ScenarioInputs.model_validate(val.inputs[k]) for k in ("bear", "base", "bull")},
                           evidence_as_of=AS_OF, label="FIXTURE")
    approve_valuation(app, vid, downside_reviewed=True)
    if approve_profile_b:
        approve_profile(app, create_profile(app, b, current_profile(app, a["security_id"])[1], change_reason="x",
                                            label="FIXTURE"))
    set_watchlist(app, b, "APPROVED")
    return a["issuer_id"], a["security_id"], b


def _issuer_weight(app, p, view, issuer_id, lines):
    cur = view.issuer_weights.get(issuer_id, Dec(0)) * view.nav
    fees = sum((Dec(str(l["fee"])) for l in lines), Dec(0))
    add = sum((Dec(str(l["amount"])) for l in lines if l["issuer"] == issuer_id), Dec(0))
    return (cur + add) / (p.nav_after - fees)


@pytest.mark.parametrize("approve_b", [True, False])
def test_share_classes_share_one_issuer_limit_in_both_variants(app, approve_b):
    from equity_monitor.data.securities import security_ref
    app.clock.set(AS_OF)
    d = build_demo(app)
    issuer, a_sid, b_sid = _add_share_class(app, d, approve_profile_b=approve_b)
    pf = d["portfolio_id"]
    p = propose(app, pf)
    view = portfolio_view(app, pf)
    pol = app.policy.portfolio

    def rows(lines):
        return [{"symbol": l["symbol"], "amount": l["amount"], "fee": l["fee"],
                 "issuer": security_ref(app.conn, app.conn.execute("SELECT id FROM security WHERE symbol=?",
                                                                   (l["symbol"],)).fetchone()["id"]).issuer_id}
                for l in lines]
    aug = rows([{"symbol": l.symbol, "amount": l.amount, "fee": l.fee} for l in p.lines if l.amount > 0])
    base = rows(p.baseline["lines"])
    for variant in (aug, base):
        w = _issuer_weight(app, p, view, issuer, variant)
        assert w <= pol.target_position_weight + Dec("0.0001"), (variant, w)    # was 12.89% before the repair
    if approve_b:
        # displayed weight is the aggregate issuer weight, identical for both classes
        ws = {l.symbol: l.proposed_weight for l in p.lines if l.symbol in ("ZZADD", "ZZADD.B")}
        assert len(set(ws.values())) == 1
    else:
        assert "ZZADD.B" not in [l["symbol"] for l in aug]                      # PAUSED: no approved exposure profile
        assert {l["symbol"] for l in base} >= {"ZZADD"}


def test_proposal_is_revalidated_after_fees_and_rounding(app):
    from equity_monitor.data.securities import security_ref
    app.clock.set(AS_OF)
    d = build_demo(app)
    app.policy = Policy(portfolio=PortfolioPolicy(target_position_weight=Dec("0.10"), max_issuer_weight=Dec("0.10"),
                                                  fee_per_trade_usd=Dec("150"), fractional_shares=False))
    pf = d["portfolio_id"]
    p = propose(app, pf)
    view = portfolio_view(app, pf)
    active = [l for l in p.lines if l.amount > 0]
    assert active
    fees = sum((l.fee for l in active), Dec(0))
    assert p.nav_after_fees == p.nav_after - fees
    for l in active:
        assert l.shares == l.shares.to_integral_value()
        w = (view.issuer_weights.get(security_ref(app.conn, l.security_id).issuer_id, Dec(0)) * view.nav + l.amount) / p.nav_after_fees
        assert w <= Dec("0.10"), (l.symbol, w)
        assert abs(l.proposed_weight - w) < Dec("0.000001")
    assert p.remaining_cash >= 0 and sum((l.amount for l in active), Dec(0)) + fees <= p.budget
    assert any("revalidation" in l.note for l in p.lines)          # the fee pass actually had to cut a line


def test_allocation_revalidates_stale_recommendations(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    pf = d["portfolio_id"]
    review_portfolio(app, pf)
    app.clock.set(AS_OF + timedelta(days=2))            # no new prices or filings checks since the review
    p = propose(app, pf)
    assert [l for l in p.lines if l.amount > 0] == []                      # was: 7-day-old ADDs reused
    reasons = {e["symbol"]: e["reason"] for e in p.excluded}
    assert "STALE_PRICE" in reasons["ZZADD"] and "REVIEW" in reasons["ZZADD"]
    assert all(v["recommendation_as_of"] <= p.as_of for v in p.validated)


def test_allocation_sees_changed_eligibility_and_new_evidence(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    pf = d["portfolio_id"]
    review_portfolio(app, pf)
    new = d["securities"]["ZZNEW"]
    # eligibility change after the review: the exposure approval is withdrawn (profile missing -> PAUSED)
    app.conn.execute("DELETE FROM exposure_approval WHERE exposure_version_id IN "
                     "(SELECT id FROM exposure_profile_version WHERE security_id=?)", (new["security_id"],))
    # new evidence after the review: a 10-Q for ZZADD becomes public before the cutoff
    add_fact(app, d["securities"]["ZZADD"]["issuer_id"], "revenue", 1, start=date(2026, 7, 1), end=date(2026, 9, 30),
             public_at=AS_OF + timedelta(minutes=30), accession="new-10q", form="10-Q")
    app.clock.set(AS_OF + timedelta(hours=1))           # same session: prices are still current
    p = propose(app, pf)
    bought = {l.symbol for l in p.lines if l.amount > 0}
    assert "ZZNEW" not in bought and "ZZADD" not in bought
    reasons = {e["symbol"]: e["reason"] for e in p.excluded}
    assert "PAUSED" in reasons["ZZNEW"]
    assert "NEW_FINANCIALS" in reasons["ZZADD"] or "REVIEW" in reasons["ZZADD"]


def test_allocation_uses_current_policy_and_rejects_future_cutoff(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    pf = d["portfolio_id"]
    review_portfolio(app, pf)
    old_policy = app.policy_version_id()
    app.policy = Policy(portfolio=PortfolioPolicy(max_issuer_weight=Dec("0.03"), target_position_weight=Dec("0.02")))
    p = propose(app, pf)
    assert {v["policy_version_id"] for v in p.validated} == {app.policy_version_id()} != {old_policy}
    assert "ZZADD" not in {l.symbol for l in p.lines if l.amount > 0}       # ZZADD is above the new 3% issuer limit
    with pytest.raises(ValueError, match="future"):
        propose(app, pf, as_of=AS_OF + timedelta(days=1))
    # a recommendation dated after the cutoff is never used for an earlier allocation
    sid = d["securities"]["ZZNEW"]["security_id"]
    app.clock.set(AS_OF + timedelta(days=1))
    future = generate(app, pf, sid)
    p2 = propose(app, pf, as_of=AS_OF)
    assert future not in {v["recommendation_id"] for v in p2.validated}


# ------------------------------------------------------------------ #4 paper execution
from equity_monitor.data.prices import Bar, PriceFetch, store_fetch
from equity_monitor.evaluation.paper import PaperError, paper_execute, paper_execute_allocation
from equity_monitor.ledger.csv_import import import_csv
from equity_monitor.ledger.store import NewEvent, create_account, create_portfolio, record_events

FILL = date(2026, 10, 1)


def _paper_book(app, name, csv_body="2026-09-01,DEPOSIT,,,,100000\n"):
    p = create_portfolio(app, name, "PAPER")
    a = create_account(app, p, "paper")
    import_csv(app, a, text="date,type,symbol,quantity,price,amount\n" + csv_body)
    return p, a


def _open_bars(app, d, syms, open_=Dec("10")):
    for s in syms:
        store_fetch(app, d["securities"][s]["security_id"], PriceFetch([Bar(FILL, open_, open=open_)]), "fixture")


def _frozen_demo(app, **portfolio):
    app.clock.set(AS_OF)
    d = build_demo(app)
    app.policy = Policy(status="FROZEN", portfolio=PortfolioPolicy(**portfolio) if portfolio else PortfolioPolicy())
    return d


def _cash(app, pf):
    return portfolio_view(app, pf, FILL).cash


def test_paper_allocation_never_borrows(app):
    d = _frozen_demo(app)
    prop = propose(app, d["portfolio_id"])
    assert sum((l.amount for l in prop.lines), Dec(0)) > Dec(3000)
    _open_bars(app, d, ("ZZADD", "ZZNEW"))
    empty, _ = _paper_book(app, "unfunded", "")
    assert paper_execute_allocation(app, prop.id, empty, "augmented") == []      # was: bought both, cash -13,281
    assert _cash(app, empty) == 0
    small, _ = _paper_book(app, "small", "2026-09-01,DEPOSIT,,,,3000\n")
    bought = paper_execute_allocation(app, prop.id, small, "augmented")
    assert bought and _cash(app, small) >= 0
    import json
    fills = json.loads(app.conn.execute("SELECT fills_json FROM paper_allocation_execution WHERE paper_portfolio_id=?",
                                        (small,)).fetchone()[0])
    # limits are measured on the paper book: 10% of a 3,000 book, not the proposal's 5,040 / 8,240
    assert all(f["binding"] == "ISSUER_LIMIT" and Dec(f["amount"]) <= Dec(300) for f in fills)


def test_paper_allocation_charges_fees_and_respects_share_rounding(app):
    d = _frozen_demo(app, fractional_shares=False)
    app.policy = app.policy.model_copy(update={"paper": app.policy.paper.model_copy(update={"fee_per_trade_usd": Dec("25")})})
    prop = propose(app, d["portfolio_id"])
    _open_bars(app, d, ("ZZADD", "ZZNEW"), open_=Dec("33.33"))
    book, acct = _paper_book(app, "fees", "2026-09-01,DEPOSIT,,,,5000\n")
    app.policy = app.policy.model_copy(update={"paper": app.policy.paper.model_copy(update={"fee_per_trade_usd": Dec("0")})})
    with pytest.raises(PaperError, match="differs from the FROZEN policy"):
        paper_execute_allocation(app, prop.id, book, "augmented")                   # bound to the originating policy
    app.policy = app.policy.model_copy(update={"paper": app.policy.paper.model_copy(update={"fee_per_trade_usd": Dec("25")})})
    bought = paper_execute_allocation(app, prop.id, book, "augmented")
    rows = app.conn.execute("SELECT quantity, price, fees FROM ledger_event WHERE account_id=? AND event_type='BUY'",
                            (acct,)).fetchall()
    assert len(rows) == len(bought) >= 1
    spent = sum(Dec(r["quantity"]) * Dec(r["price"]) + Dec(r["fees"]) for r in rows)
    assert all(Dec(r["quantity"]) == Dec(r["quantity"]).to_integral_value() and Dec(r["fees"]) == 25 for r in rows)
    assert _cash(app, book) == Dec(5000) - spent >= 0


def test_paper_allocation_is_idempotent_and_atomic(app):
    d = _frozen_demo(app)
    prop = propose(app, d["portfolio_id"])
    book, acct = _paper_book(app, "idem")
    _open_bars(app, d, ("ZZADD",))                      # ZZNEW's fill bar is missing: nothing may be recorded
    assert paper_execute_allocation(app, prop.id, book, "augmented") == []
    assert app.conn.execute("SELECT COUNT(*) FROM ledger_event WHERE account_id=? AND event_type='BUY'", (acct,)).fetchone()[0] == 0
    _open_bars(app, d, ("ZZNEW",))
    first = paper_execute_allocation(app, prop.id, book, "augmented")
    assert first == ["ZZADD", "ZZNEW"]
    n = app.conn.execute("SELECT COUNT(*) FROM ledger_event WHERE account_id=?", (acct,)).fetchone()[0]
    for _ in range(3):
        assert paper_execute_allocation(app, prop.id, book, "augmented") == first
    assert app.conn.execute("SELECT COUNT(*) FROM ledger_event WHERE account_id=?", (acct,)).fetchone()[0] == n
    with pytest.raises(Exception):
        app.conn.execute("DELETE FROM paper_allocation_execution")


def test_paper_respects_variant_eligibility(app):
    d = _frozen_demo(app)
    new = d["securities"]["ZZNEW"]["security_id"]
    app.conn.execute("DELETE FROM exposure_approval WHERE exposure_version_id IN "
                     "(SELECT id FROM exposure_profile_version WHERE security_id=?)", (new,))
    prop = propose(app, d["portfolio_id"])
    rec = get(app, next(v["recommendation_id"] for v in prop.validated if v["symbol"] == "ZZNEW"))
    assert (rec["action"], rec["purchase_eligibility"], rec["baseline_eligibility"]) == ("ADD", "PAUSED", "ELIGIBLE")
    with pytest.raises(PaperError, match="ADD is paper-executed only through an allocation"):
        paper_execute(app, rec["id"], _paper_book(app, "direct")[0])            # was: PAUSED ADD filled anyway
    _open_bars(app, d, ("ZZADD", "ZZNEW"))
    aug, _ = _paper_book(app, "aug")
    base, _ = _paper_book(app, "base")
    assert "ZZNEW" not in paper_execute_allocation(app, prop.id, aug, "augmented")
    assert "ZZNEW" in paper_execute_allocation(app, prop.id, base, "baseline")


def test_record_events_can_refuse_negative_cash(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    book, acct = _paper_book(app, "neg", "2026-09-01,DEPOSIT,,,,100\n")
    ev = NewEvent("BUY", FILL, d["securities"]["ZZADD"]["security_id"], quantity=Dec(20), price=Dec(10), fees=Dec(0),
                  external_id="x1")
    assert record_events(app, acct, [ev], allow_negative_cash=False).rejected
    assert record_events(app, acct, [ev]).inserted                   # broker import path: kept, reconciliation issue


def test_paper_trim_sells_to_documented_target_weight(app):
    d = _frozen_demo(app)
    sid = d["securities"]["ZZTRM"]["security_id"]
    rec = get(app, generate(app, d["portfolio_id"], sid))
    assert rec["action"] == "TRIM"
    tw = Dec(str(rec["payload"]["proposed_trade"]["target_weight"]))
    book, acct = _paper_book(app, "trim", "2026-09-01,DEPOSIT,,,,6000\n2026-09-02,BUY,ZZTRM,100,40,\n")
    store_fetch(app, sid, PriceFetch([Bar(FILL, Dec("40"), open=Dec("40"))]), "fixture")
    pv = portfolio_view(app, book, date(2026, 9, 30))
    assert pv.holding(sid).shares == 100
    assert paper_execute(app, rec["id"], book)
    sold = Dec(app.conn.execute("SELECT quantity FROM ledger_event WHERE account_id=? AND event_type='SELL'",
                                (acct,)).fetchone()[0])
    assert sold != 50                                                   # was: always half the position
    px = Dec("40") * (1 - app.policy.paper.slippage_bps / 10000)
    nav = pv.cash + 100 * px
    w_after = (100 - sold) * px / nav
    assert w_after <= tw and w_after > tw - px / nav * 2                # at the target, within one-share rounding
    assert paper_execute(app, rec["id"], book) is None                  # at most once


# ------------------------------------------------------------------ #6 benchmark replayed from inception
from equity_monitor.data.prices import Action
from equity_monitor.evaluation.performance import contribution_matched, performance


@pytest.fixture
def bm(app):
    from equity_monitor.data.securities import register_security
    sid = register_security(app.conn, app.now_iso(), "BM", security_type="ETF")
    bars = [Bar(date(2026, 3, 2), Dec(100)), Bar(date(2026, 3, 3), Dec(100)), Bar(date(2026, 3, 4), Dec(50)),
            Bar(date(2026, 3, 5), Dec(50)), Bar(date(2026, 3, 6), Dec(55))]
    acts = [Action("SPLIT", date(2026, 3, 4), Dec(2), Dec(1)), Action("CASH_DIVIDEND", date(2026, 3, 5), cash_amount=Dec(1))]
    store_fetch(app, sid, PriceFetch(bars, acts), "fixture")

    def book(name, csv):
        p = create_portfolio(app, name, "ACTUAL")
        import_csv(app, create_account(app, p, "a"), text="date,type,amount\n" + csv)
        return p
    return book


def test_subperiod_benchmark_keeps_earlier_funding(app, bm):
    # 2026-03-01 is a Sunday: the deposit executes on Monday's session
    pf = bm("b1", "2026-03-01,DEPOSIT,1000\n2026-03-04,DEPOSIT,500\n2026-03-06,WITHDRAWAL,300\n")
    full = dict(contribution_matched(app, pf, "BM", date(2026, 3, 1), date(2026, 3, 6)).values)
    later = contribution_matched(app, pf, "BM", date(2026, 3, 5), date(2026, 3, 6))
    assert later.mode == "inception" and later.replay_start == date(2026, 3, 1)
    assert dict(later.values) == {d: v for d, v in full.items() if d >= date(2026, 3, 5)}   # was: 0 -> -300
    assert dict(later.values)[date(2026, 3, 5)] == Dec(1530)
    assert later.flows_applied[0]["date"] == date(2026, 3, 2)
    rebased = contribution_matched(app, pf, "BM", date(2026, 3, 5), date(2026, 3, 6), mode="rebased")
    # starts from the portfolio NAV on 03-04 (1500, all cash) bought at the 03-05 close of 50 -> 30 units
    assert rebased.mode == "rebased" and dict(rebased.values)[date(2026, 3, 5)] == Dec(1500)
    assert round(dict(rebased.values)[date(2026, 3, 6)], 6) == Dec(1350)
    rep = performance(app, pf, date(2026, 3, 5), date(2026, 3, 6), benchmarks=["BM"])
    assert rep["benchmarks"]["BM"]["start_value"] == Dec(1530) and rep["benchmarks"]["BM"]["mode"] == "inception"


def test_withdrawal_larger_than_benchmark_empties_it(app, bm):
    pf = bm("b2", "2026-03-02,DEPOSIT,1000\n2026-03-04,WITHDRAWAL,900\n")
    b = contribution_matched(app, pf, "BM", date(2026, 3, 2), date(2026, 3, 6))
    # 10 units -> split 20 units at 50 = 1000; withdraw 900 -> 2 units; no negative units ever
    assert dict(b.values)[date(2026, 3, 4)] == Dec(100)
    pf2 = bm("b3", "2026-03-02,DEPOSIT,1000\n2026-03-04,WITHDRAWAL,1200\n")
    b2 = contribution_matched(app, pf2, "BM", date(2026, 3, 2), date(2026, 3, 6))
    assert b2.units == 0 and dict(b2.values)[date(2026, 3, 4)] == 0 and any("emptied" in w for w in b2.warnings)


def test_missing_benchmark_bars_execute_late_or_stay_unapplied(app, bm):
    from equity_monitor.data.securities import register_security
    sid = register_security(app.conn, app.now_iso(), "BM2", security_type="ETF")
    store_fetch(app, sid, PriceFetch([Bar(date(2026, 3, 2), Dec(100)), Bar(date(2026, 3, 4), Dec(100))]), "fixture")
    pf = bm("b4", "2026-03-02,DEPOSIT,1000\n2026-03-03,DEPOSIT,500\n2026-03-05,DEPOSIT,200\n")
    b = contribution_matched(app, pf, "BM2", date(2026, 3, 2), date(2026, 3, 6))
    assert dict(b.values)[date(2026, 3, 4)] == Dec(1500)
    assert any("late" in w and "2026-03-03" in w for w in b.warnings)
    assert [u["amount"] for u in b.unapplied] == [Dec(200)]


# ------------------------------------------------------------------ #7 eligibility transitions alert
def _daily(app, prov, force=False):
    from equity_monitor.monitoring import scheduler as sch
    from equity_monitor.monitoring.jobs import JobContext, handlers
    ctx = JobContext(price_provider=prov, refresh_market_series=False)
    spec = sch.DEFAULT_JOBS[0]
    sch.run_instance(app, spec, sch.latest_due(spec, app.now()), handlers(ctx)["daily_refresh"], force=force)


def _alerts(app):
    return [dict(r) for r in app.conn.execute("SELECT alert_key, title, body_md, severity FROM alert "
                                              "WHERE kind='MATERIAL_EVENT' ORDER BY created_at")]


def test_eligibility_transitions_raise_explained_alerts_once(app):
    from equity_monitor.data.prices import FixturePriceProvider
    from equity_monitor.decisions.recommend import latest_for
    from equity_monitor.fixtures import build_market_fixture
    from equity_monitor.market.exposures import Exposure, approve_profile, create_profile, current_profile
    app.clock.set(AS_OF)
    d = build_demo(app)
    build_market_fixture(app)
    prov = FixturePriceProvider({s: PriceFetch([Bar(date(2026, 9, 30), v.get("price") or Dec(30))])
                                 for s, v in d["securities"].items()})
    _daily(app, prov)
    s = d["securities"]["ZZADD"]["security_id"]
    assert latest_for(app, d["portfolio_id"], s)["purchase_eligibility"] == "ELIGIBLE"
    n0 = len(_alerts(app))
    original = current_profile(app, s)[1]
    prof = original.with_exposure(Exposure(factor="REFINANCING", direction="NEGATIVE", magnitude="HIGH",
                                                             mechanism="x", basis="ANALYST_ASSUMPTION"))
    approve_profile(app, create_profile(app, s, prof, change_reason="x", label="FIXTURE"))
    # ELIGIBLE -> PAUSED (action unchanged: ADD)
    app.clock.set(AS_OF + timedelta(hours=1))
    build_market_fixture(app, as_of=app.now(), hy_level=Dec("6.5"))
    _daily(app, prov, force=True)
    r = latest_for(app, d["portfolio_id"], s)
    assert (r["action"], r["purchase_eligibility"]) == ("ADD", "PAUSED")
    new = _alerts(app)[n0:]
    assert [a["title"] for a in new] == ["ZZADD: purchases ELIGIBLE → PAUSED"]            # was: no alert
    body = new[0]["body_md"]
    assert "PAUSED" in body and "Reassess:" in body and "Evidence:" in body
    # repeated unchanged runs: no new alert (dedup by recommendation/event key)
    _daily(app, prov, force=True)
    app.clock.set(AS_OF + timedelta(hours=2))
    _daily(app, prov, force=True)
    assert len(_alerts(app)) == n0 + 1
    # PAUSED -> ELIGIBLE: the owner reassesses and re-approves the profile without the HIGH refinancing exposure
    app.clock.set(AS_OF + timedelta(hours=3))
    approve_profile(app, create_profile(app, s, original, change_reason="reassessed: refinancing exposure LOW",
                                        label="FIXTURE"))
    _daily(app, prov, force=True)
    assert latest_for(app, d["portfolio_id"], s)["purchase_eligibility"] == "ELIGIBLE"
    last = _alerts(app)[n0 + 1:]
    assert [a["title"] for a in last] == ["ZZADD: purchases PAUSED → ELIGIBLE"]
    assert "no longer applies" in last[0]["body_md"]
    # notification authorization is preserved: nothing leaves the machine without it
    sent = app.conn.execute("SELECT COUNT(*) FROM delivery_attempt").fetchone()[0]
    assert sent == 0


# ------------------------------------------------------------------ augmented evaluation is descriptive
def test_augmented_evaluation_uses_episodes_fixed_horizon_and_no_cumulative_cash(app):
    from equity_monitor.evaluation.augmented import HORIZON_SESSIONS, _horizon_end, compare, pause_episodes
    from equity_monitor.fixtures import build_market_fixture
    from equity_monitor.market.exposures import Exposure, approve_profile, create_profile, current_profile
    app.clock.set(AS_OF)
    d = build_demo(app)
    build_market_fixture(app, hy_level=Dec("6.5"))
    pf, s = d["portfolio_id"], d["securities"]["ZZADD"]["security_id"]
    original = current_profile(app, s)[1]
    approve_profile(app, create_profile(app, s, original.with_exposure(Exposure(
        factor="REFINANCING", direction="NEGATIVE", magnitude="HIGH", mechanism="x", basis="ANALYST_ASSUMPTION")),
        change_reason="high", label="FIXTURE"))
    for h in range(3):                                  # three rows of the same pause = one episode
        app.clock.set(AS_OF + timedelta(minutes=10 * h))
        generate(app, pf, s, force=True)
        propose(app, pf)                                # three proposals withholding the same cash
    app.clock.set(AS_OF + timedelta(minutes=40))
    approve_profile(app, create_profile(app, s, original, change_reason="reassessed", label="FIXTURE"))
    assert get(app, generate(app, pf, s))["purchase_eligibility"] == "ELIGIBLE"
    eps = [e for e in pause_episodes(app, pf) if e["symbol"] == "ZZADD"]
    assert len(eps) == 1 and eps[0]["rows"] >= 3 and eps[0]["end"] is not None
    ev = compare(app, pf)
    per = ev["cash_withheld_vs_baseline_per_proposal"]
    assert len(per) == 3 and "cash_withheld_vs_baseline_total" not in ev              # never summed
    assert ev["cash_withheld_vs_baseline_latest"] == per[-1]["cash_withheld_vs_baseline"]
    assert ev["matured_episodes"] == [] and ev["verdict"].startswith("insufficient evidence")
    assert "descriptive" in ev["verdict"]
    assert _horizon_end(date(2026, 9, 30)) == date(2026, 12, 30) and HORIZON_SESSIONS == 63


def test_monthly_allocation_revalidation_alerts_changed_eligibility(app):
    from equity_monitor.data.prices import FixturePriceProvider
    from equity_monitor.monitoring.jobs import JobContext, handlers
    app.clock.set(AS_OF)
    d = build_demo(app)
    prov = FixturePriceProvider({s: PriceFetch([Bar(date(2026, 9, 30), v.get("price") or Dec(30))])
                                 for s, v in d["securities"].items()})
    _daily(app, prov)
    new = d["securities"]["ZZNEW"]["security_id"]
    app.conn.execute("DELETE FROM exposure_approval WHERE exposure_version_id IN "
                     "(SELECT id FROM exposure_profile_version WHERE security_id=?)", (new,))
    handlers(JobContext(price_provider=prov, refresh_market_series=False))["monthly_allocation"](app, None, app.now())
    titles = [a["title"] for a in _alerts(app)]
    assert "ZZNEW: purchases ELIGIBLE → PAUSED" in titles          # the allocation's re-review is not silent


# ================================================================== follow-up review of af00fc1
# ------------------------------------------------------------------ R1 relationships between quantities
FROM_TO = "Revenue increased from $3 billion to $4 billion in 2025."
CMP = "Revenue was $4.2 billion in fiscal 2025 compared with $3.75 billion in fiscal 2024, an increase of 12%."


@pytest.fixture
def rel(app):
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="RelCo", cik="940")
    return iss, {"ft": _doc(app, iss, FROM_TO, doc_id="doc_ft"), "cmp": _doc(app, iss, CMP, doc_id="doc_cmp")}


def _rv(app, rel, key, claim):
    iss, pids = rel
    return _v(app, iss, claim, FROM_TO if key == "ft" else CMP, pids[key])


def test_swapped_from_to_values_fail(app, rel):
    v = _rv(app, rel, "ft", "Revenue increased from $4 billion to $3 billion in 2025.")   # was VERIFIED
    assert v.status == "FAILED" and any("inconsistent" in d for d in v.details)
    v2 = _rv(app, rel, "ft", "Revenue went from $4 billion to $3 billion in 2025.")       # no direction word
    assert v2.status == "FAILED" and v2.support_status == "CONTRADICTED"
    assert _rv(app, rel, "ft", FROM_TO).status == "VERIFIED"


def test_comparison_value_presented_as_current_result_fails(app, rel):
    v = _rv(app, rel, "ft", "Revenue was $3 billion in 2025.")                            # was VERIFIED
    assert v.status == "FAILED" and any("value/scale" in d for d in v.details)
    assert _rv(app, rel, "ft", "Revenue was $4 billion in 2025.").status == "VERIFIED"
    assert _rv(app, rel, "cmp", "Revenue was $3.75 billion in fiscal 2025.").status == "FAILED"


def test_prior_and_current_periods(app, rel):
    # a comparison value with its own stated period is that period's level
    assert _rv(app, rel, "cmp", "Revenue was $3.75 billion in fiscal 2024.").status == "VERIFIED"
    assert _rv(app, rel, "cmp", "Revenue was $4.2 billion in fiscal 2024.").status == "FAILED"
    # the source never states which period "$3 billion" belongs to: not verifiable as 2024 revenue
    assert _rv(app, rel, "ft", "Revenue in 2024 was $3 billion.").status == "SOURCE_MATCHED"


def test_levels_versus_changes(app, rel):
    assert _rv(app, rel, "cmp", "Revenue increased 12% to $4.2 billion in fiscal 2025.").status == "VERIFIED"
    assert _rv(app, rel, "cmp", "Revenue was $4.2 billion in fiscal 2025, up 12%.").status == "VERIFIED"
    assert _rv(app, rel, "cmp", "Revenue increased by $4.2 billion in fiscal 2025.").status == "SOURCE_MATCHED"  # level as change
    assert _rv(app, rel, "ft", "Revenue grew $3 billion in 2025.").status == "SOURCE_MATCHED"                 # prior as change
    assert _rv(app, rel, "cmp", "Revenue was 12% in fiscal 2025.").status != "VERIFIED"                       # change as level
    # wording whose relationship cannot be determined is never verified
    v = _rv(app, rel, "ft", "Revenue, $4 billion in 2025.")
    assert v.status == "SOURCE_MATCHED" and any("relationship" in d for d in v.details)


def test_relationship_failures_reach_the_approval_gates(app):
    from equity_monitor.research.thesis import ThesisEvidenceError, approve_version
    app.clock.set(AS_OF)
    d = build_demo(app)
    sid, iss = d["securities"]["ZZHLD"]["security_id"], d["securities"]["ZZHLD"]["issuer_id"]
    pid = _doc(app, iss, FROM_TO, doc_id="doc_rel", public_at="2026-08-01T21:00:00.000000Z")
    swapped = _thesis_with(app, sid, [ClaimIn(text="Revenue increased from $4 billion to $3 billion in 2025.",
                                              claim_type="FACT", citations=[Citation(passage_id=pid, quote=FROM_TO)])])
    with pytest.raises(ThesisEvidenceError, match="failed verification"):
        approve_version(app, swapped, acknowledge_unverified=True)
    vague = _thesis_with(app, sid, [ClaimIn(text="Revenue in 2024 was $3 billion.", claim_type="FACT",
                                            citations=[Citation(passage_id=pid, quote=FROM_TO)])])
    with pytest.raises(ThesisEvidenceError, match="not substantively verified"):
        approve_version(app, vague)


# ------------------------------------------------------------------ R2 execution state across proposals in one session
def _weights(app, pf):
    from equity_monitor.data.securities import security_ref
    rows = app.conn.execute("SELECT e.security_id, e.event_type, e.quantity, e.price, e.fees, e.amount FROM ledger_event e "
                            "JOIN account a ON a.id=e.account_id WHERE a.portfolio_id=? ORDER BY e.seq", (pf,)).fetchall()
    cash, shares, px = Dec(0), {}, {}
    for r in rows:
        if r["event_type"] == "DEPOSIT":
            cash += Dec(r["amount"])
        elif r["event_type"] in ("BUY", "SELL"):
            q, p, f = Dec(r["quantity"]), Dec(r["price"]), Dec(r["fees"] or 0)
            sign = 1 if r["event_type"] == "BUY" else -1
            cash -= sign * q * p + f
            shares[r["security_id"]] = shares.get(r["security_id"], Dec(0)) + sign * q
            px[r["security_id"]] = p
    nav = cash + sum(shares[s] * px[s] for s in shares)
    iw = {}
    for s, q in shares.items():
        k = security_ref(app.conn, s).issuer_id
        iw[k] = iw.get(k, Dec(0)) + q * px[s] / nav
    return cash, iw


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_two_proposals_in_one_session_cannot_breach_limits(app, order):
    d = _frozen_demo(app)
    props = [propose(app, d["portfolio_id"]), propose(app, d["portfolio_id"])]
    assert props[0].id != props[1].id
    _open_bars(app, d, ("ZZADD", "ZZNEW"))
    book, _ = _paper_book(app, "shared", "2026-09-01,DEPOSIT,,,,3000\n")
    first, second = (props[i] for i in order)
    assert paper_execute_allocation(app, first.id, book, "augmented")
    with pytest.raises(PaperError, match="mutually exclusive"):
        paper_execute_allocation(app, second.id, book, "augmented")          # was: bought both again (19.994% each)
    assert paper_execute_allocation(app, first.id, book, "augmented")        # same-proposal idempotency preserved
    cash, iw = _weights(app, book)
    assert cash >= 0 and all(w <= Dec("0.10") for w in iw.values()), iw
    with pytest.raises(PaperError, match="mutually exclusive"):               # the other variant in the same book, too
        paper_execute_allocation(app, first.id, book, "baseline")


def test_execution_state_includes_same_session_fills_at_open_time_prices(app):
    from equity_monitor.evaluation.paper import _book_state
    d = _frozen_demo(app)
    sid = d["securities"]["ZZADD"]["security_id"]
    book, acct = _paper_book(app, "state", "2026-09-01,DEPOSIT,,,,3000\n")
    store_fetch(app, sid, PriceFetch([Bar(FILL, Dec("100"), open=Dec("10"))]), "fixture")   # close 100: must not be used
    record_events(app, acct, [NewEvent("BUY", FILL, sid, quantity=Dec(20), price=Dec(10), fees=Dec(0), external_id="early")],
                  allow_negative_cash=False)
    st = _book_state(app, book, acct, FILL)
    assert st.shares[sid] == 20 and st.value[sid] == Dec(200) and st.nav == Dec(3000)
    assert st.issuer_value[d["securities"]["ZZADD"]["issuer_id"]] == Dec(200)


def test_paper_book_limits_aggregate_share_classes_with_fees(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    issuer, a_sid, b_sid = _add_share_class(app, d)
    app.policy = Policy(status="FROZEN")
    app.policy = app.policy.model_copy(update={"paper": app.policy.paper.model_copy(update={"fee_per_trade_usd": Dec("5")})})
    prop = propose(app, d["portfolio_id"])
    assert "ZZADD" in {l.symbol for l in prop.lines if l.amount > 0}
    _open_bars(app, d, ("ZZADD", "ZZNEW"))
    store_fetch(app, b_sid, PriceFetch([Bar(FILL, Dec("10"), open=Dec("10"))]), "fixture")
    # earlier in the SAME session the book bought 9% of NAV in the other share class (at that session's open)
    book, acct = _paper_book(app, "classes", "2026-09-01,DEPOSIT,,,,3000\n")
    record_events(app, acct, [NewEvent("BUY", FILL, b_sid, quantity=Dec(27), price=Dec(10), fees=Dec(0),
                                       external_id="classB")], allow_negative_cash=False)
    paper_execute_allocation(app, prop.id, book, "augmented")
    import json
    row = app.conn.execute("SELECT fills_json, skipped_json FROM paper_allocation_execution WHERE paper_portfolio_id=?",
                           (book,)).fetchone()
    fills, skipped = json.loads(row[0]), json.loads(row[1])
    # issuer room left is 10% x 3,000 - 270 = 30 (< $50 minimum): the share class is not bought again
    assert "ZZADD" not in {f["symbol"] for f in fills}                         # was: ~$300 more (19% issuer weight)
    assert any(s_["symbol"] == "ZZADD" and "ISSUER_LIMIT" in s_["reason"] for s_ in skipped)
    cash, iw = _weights(app, book)
    assert cash >= 0 and iw[issuer] <= Dec("0.10") and all(w <= Dec("0.10") for w in iw.values())


# ------------------------------------------------------------------ R3 sell identity is per paper portfolio
def _two_books_holding(app, d, sym, qty, px):
    return {n: _paper_book(app, n, f"2026-09-01,DEPOSIT,,,,6000\n2026-09-02,BUY,{sym},{qty},{px},\n")[0]
            for n in ("augmented", "baseline")}


@pytest.mark.parametrize("sym,action", [("ZZTRM", "TRIM"), ("ZZEXT", "EXIT")])
def test_sell_recommendation_executes_once_in_each_paper_book(app, sym, action):
    d = _frozen_demo(app)
    sid = d["securities"][sym]["security_id"]
    rec = get(app, generate(app, d["portfolio_id"], sid))
    assert rec["action"] == action
    books = _two_books_holding(app, d, sym, 100, 40)
    store_fetch(app, sid, PriceFetch([Bar(FILL, Dec("40"), open=Dec("40"))]), "fixture")
    ids = {n: paper_execute(app, rec["id"], p) for n, p in books.items()}
    assert all(ids.values()) and ids["augmented"] != ids["baseline"]          # was: baseline returned None
    left = {n: portfolio_view(app, p, FILL).holding(sid) for n, p in books.items()}
    if action == "EXIT":
        assert all(h is None or h.shares == 0 for h in left.values())
    else:
        assert left["augmented"].shares == left["baseline"].shares < 100
    for p in books.values():                                                   # never twice in either book
        assert paper_execute(app, rec["id"], p) is None
    assert app.conn.execute("SELECT COUNT(*) FROM paper_execution WHERE recommendation_id=?", (rec["id"],)).fetchone()[0] == 2


def test_paper_execution_scope_migration_preserves_rows(monkeypatch, tmp_path):
    import sqlite3
    from equity_monitor.app import memory_app
    from equity_monitor.db import core
    from equity_monitor.util import Clock
    orig = core._migration_files
    monkeypatch.setattr(core, "_migration_files", lambda: [f for f in orig() if f[0] < "0006"])
    app = memory_app(clock=Clock(AS_OF), home=tmp_path)                       # schema as of af00fc1
    d = build_demo(app)
    app.policy = Policy(status="FROZEN")
    sid = d["securities"]["ZZTRM"]["security_id"]
    rec = generate(app, d["portfolio_id"], sid)
    books = _two_books_holding(app, d, "ZZTRM", 100, 40)
    store_fetch(app, sid, PriceFetch([Bar(FILL, Dec("40"), open=Dec("40"))]), "fixture")
    first = paper_execute(app, rec, books["augmented"])
    with pytest.raises(sqlite3.IntegrityError):                                 # old global UNIQUE(recommendation_id)
        paper_execute(app, rec, books["baseline"])
    monkeypatch.setattr(core, "_migration_files", orig)
    assert core.migrate(app.conn)[0] == "0006_paper_scope.sql"
    assert [tuple(r) for r in app.conn.execute("SELECT id, paper_portfolio_id FROM paper_execution")] == \
        [(first, books["augmented"])]                                           # existing record preserved
    assert paper_execute(app, rec, books["baseline"])
    assert paper_execute(app, rec, books["baseline"]) is None


def test_claims_verified_by_previous_verifier_are_downgraded(monkeypatch, tmp_path):
    from equity_monitor.app import memory_app
    from equity_monitor.db import core
    from equity_monitor.util import Clock
    orig = core._migration_files
    monkeypatch.setattr(core, "_migration_files", lambda: [f for f in orig() if f[0] < "0007"])
    app = memory_app(clock=Clock(AS_OF), home=tmp_path)
    for cid, ver, vv in (("c_ev2", "VERIFIED", "ev-2"), ("c_ev3", "VERIFIED", "ev-3"), ("c_fail", "FAILED", "ev-2")):
        insert(app.conn, "claim", {"id": cid, "owner_type": "THESIS_VERSION", "owner_id": "v", "text": "t",
                                   "claim_type": "FACT", "verification": ver, "verification_detail": None,
                                   "citation_status": "SOURCE_MATCHED", "support_status": "CONFIRMED" if ver == "VERIFIED" else "CONTRADICTED",
                                   "verifier_version": vv, "created_at": "t"})
    monkeypatch.setattr(core, "_migration_files", orig)
    core.migrate(app.conn)
    got = {r[0]: (r[1], r[2]) for r in app.conn.execute("SELECT id, verification, support_status FROM claim")}
    assert got == {"c_ev2": ("SOURCE_MATCHED", "LEGACY"), "c_ev3": ("VERIFIED", "CONFIRMED"),
                   "c_fail": ("FAILED", "CONTRADICTED")}
