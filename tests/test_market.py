"""Market/sector/company context: relevance, clustering, missing data, traceability, safeguards."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from equity_monitor.config.models import MarketPolicy, Policy, PortfolioPolicy
from equity_monitor.db.core import insert
from equity_monitor.decisions.allocation import propose
from equity_monitor.decisions.recommend import generate, get, review_portfolio
from equity_monitor.evaluation.augmented import compare
from equity_monitor.evaluation.paper import PaperError, paper_execute_allocation
from equity_monitor.fixtures import AS_OF, build_demo, build_market_fixture
from equity_monitor.ledger.csv_import import import_csv
from equity_monitor.ledger.store import create_account, create_portfolio
from equity_monitor.llm.base import FixtureLLM
from equity_monitor.llm.service import explain_cluster
from equity_monitor.market import series as ms
from equity_monitor.market.exposures import (
    Exposure, ExposureProfile, approve_profile, create_profile, current_profile, draft_default_profile,
)
from equity_monitor.market.external import add_external_observation
from equity_monitor.market.lookthrough import portfolio_market_exposure
from equity_monitor.market.snapshot import build_snapshot, load_snapshot
from equity_monitor.market.sources import (
    InterpretationError, assert_inference_allowed, assert_same_class, assert_use,
)
from equity_monitor.util import iso_utc

UTC = timezone.utc


@pytest.fixture
def demo(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    build_market_fixture(app)
    return d


def sec(d, sym):
    return d["securities"][sym]["security_id"]


def add_exposure(app, sid, *exps, reason="add exposure"):
    cur = current_profile(app, sid)
    prof = cur[1] if cur else draft_default_profile(app, sid)
    for e in exps:
        prof = prof.with_exposure(e)
    v = create_profile(app, sid, prof, change_reason=reason, label="FIXTURE")
    approve_profile(app, v)
    return v


HIGH_REFI = Exposure(factor="REFINANCING", direction="NEGATIVE", magnitude="HIGH", mechanism="large 2027 maturities",
                     basis="ANALYST_ASSUMPTION", valuation_assumption="wacc")
HIGH_RATES = Exposure(factor="RATES", direction="NEGATIVE", magnitude="HIGH", mechanism="long-duration cash flows",
                      basis="ANALYST_ASSUMPTION", valuation_assumption="wacc")


# ------------------------------------------------------------------ irrelevant events
def test_irrelevant_events_do_not_alter_recommendations(app, demo):
    pf, s = demo["portfolio_id"], sec(demo, "ZZADD")
    r1 = get(app, generate(app, pf, s))
    assert (r1["action"], r1["purchase_eligibility"]) == ("ADD", "ELIGIBLE")
    # oil shock + energy-sector selloff + broad market selloff: ZZADD (software) has no oil/energy exposure,
    # and broad weakness is not a reason to stop buying by default
    snap2 = build_market_fixture(app, oil_3m=D("0.60"), energy_last_month=D("-0.30"), equity_last_month=D("-0.20"),
                                 vix_level=D(38))
    assert snap2 != r1["market_snapshot_id"]
    flags = [f["flag"] for f in load_snapshot(app, snap2)["flags"]]
    assert "OIL_UP" in flags and "MARKET_DRAWDOWN" in flags
    assert generate(app, pf, s) == r1["id"]                     # no new decision row: nothing relevant changed
    fresh = get(app, generate(app, pf, s, force=True))
    assert (fresh["action"], fresh["purchase_eligibility"]) == ("ADD", "ELIGIBLE")
    oil = [c for c in fresh["payload"]["chains"] if c["cluster_key"] == "market:OIL"][0]
    assert oil["effect"] == "CONTEXT_ONLY" and oil["relevance"]["status"] == "CONTEXT"


# ------------------------------------------------------------------ relevant developments
def test_adverse_development_on_high_exposure_pauses_but_does_not_sell(app, demo):
    pf = demo["portfolio_id"]
    for sym in ("ZZADD", "ZZHLD"):
        add_exposure(app, sec(demo, sym), HIGH_REFI)
    build_market_fixture(app, hy_level=D("6.5"))
    add = get(app, generate(app, pf, sec(demo, "ZZADD")))
    hold = get(app, generate(app, pf, sec(demo, "ZZHLD")))
    assert (add["action"], add["purchase_eligibility"], add["baseline_eligibility"]) == ("ADD", "PAUSED", "ELIGIBLE")
    assert (hold["action"], hold["purchase_eligibility"]) == ("HOLD", "PAUSED")       # HOLD + PAUSED coexist
    p = add["payload"]["current_conditions"]["pauses"][0]
    assert p["code"] == "ADVERSE_REFINANCING_HIGH_EXPOSURE" and p["reassess_on"] and p["reassess_condition"]
    # allocation skips the paused name; the fundamental-only baseline would have bought it
    prop = propose(app, pf)
    assert "ZZADD" not in [l.symbol for l in prop.lines if l.amount > 0]
    assert any(e["symbol"] == "ZZADD" and "PAUSED" in e["reason"] for e in prop.excluded)
    assert "ZZADD" in [l["symbol"] for l in prop.baseline["lines"]]
    assert prop.baseline["cash_withheld_vs_baseline"] > 0
    ev = compare(app, pf)
    assert ev["pause_episodes"] >= 1 and ev["verdict"].startswith("insufficient evidence")


def test_favorable_development_is_not_a_buy_signal(app, demo):
    pf, s = demo["portfolio_id"], sec(demo, "ZZHLD")
    add_exposure(app, s, Exposure(factor="COMMODITY_OIL", direction="POSITIVE", magnitude="HIGH", mechanism="producer",
                                  basis="ANALYST_ASSUMPTION"))
    build_market_fixture(app, oil_3m=D("0.60"), equity_last_month=D("0.25"))
    r = get(app, generate(app, pf, s))
    assert r["action"] == "HOLD" and r["purchase_eligibility"] == "ELIGIBLE"
    oil = [c for c in r["payload"]["chains"] if c["cluster_key"] == "market:OIL"][0]
    assert oil["effect"] == "NO_CHANGE" and "not a reason to buy" in " ".join(oil["implication"]["detail"])


def test_market_weakness_never_liquidates_and_stress_pause_is_opt_in(app, demo):
    pf = demo["portfolio_id"]
    build_market_fixture(app, equity_last_month=D("-0.30"), vix_level=D(45))
    before = {get(app, r)["payload"]["symbol"]: get(app, r)["action"] for r in review_portfolio(app, pf)}
    assert before["ZZADD"] == "ADD" and before["ZZHLD"] == "HOLD"
    add = get(app, generate(app, pf, sec(demo, "ZZADD")))
    assert add["purchase_eligibility"] == "ELIGIBLE"
    app.policy = Policy(market=MarketPolicy(pause_on_market_stress=True))
    stressed = get(app, generate(app, pf, sec(demo, "ZZADD")))
    assert stressed["action"] == "ADD" and stressed["purchase_eligibility"] == "PAUSED"
    assert stressed["payload"]["current_conditions"]["pauses"][0]["code"] == "MARKET_STRESS_POLICY"


# ------------------------------------------------------------------ clustering / double counting
def _filing(app, issuer_id, acc, items, public_at, form="8-K"):
    insert(app.conn, "source_document", {"id": f"doc_{acc}", "provider": "FIXTURE", "doc_type": form, "issuer_id": issuer_id,
                                         "accession_no": acc, "source_url": None, "title": form, "fiscal_period_end": None,
                                         "filed_date": public_at[:10], "public_at": public_at, "public_at_basis": "PROVIDED",
                                         "retrieved_at": public_at, "raw_object_id": None, "content_hash": None,
                                         "parser_version": None, "trust": "FIXTURE", "items": items, "limitations": None})


def test_overlapping_company_evidence_is_one_development(app, demo):
    pf, s, iss = demo["portfolio_id"], sec(demo, "ZZNEW"), demo["securities"]["ZZNEW"]["issuer_id"]
    app.clock.set(AS_OF + timedelta(hours=1))                     # the owner's approvals happened at AS_OF
    _filing(app, iss, "FX-8K-1", "2.02,9.01", "2026-09-30T22:30:00.000000Z")          # weak results, after approval
    si = ms.short_interest_spec("ZZNEW")
    ms.store_values(app, si, [(date(2026, 9, 15), D(1_000_000))], raw_id=None, extra={date(2026, 9, 15): {"days_to_cover": "3"}})
    ms.store_values(app, si, [(date(2026, 9, 28), D(1_900_000))], raw_id=None, extra={date(2026, 9, 28): {"days_to_cover": "12"}},
                    public_at_override={date(2026, 9, 28): datetime(2026, 9, 30, 22, 10, tzinfo=UTC)})
    ms.store_values(app, ms.short_volume_spec("ZZNEW"), [(date(2026, 9, 28), D("0.62"))], raw_id=None)
    add_external_observation(app, s, source_name="Some newswire", text="Analysts say demand is collapsing", url="https://x.test",
                             published_at=datetime(2026, 9, 29, 12, tzinfo=UTC))
    r = get(app, generate(app, pf, s))
    event = [c for c in r["payload"]["chains"] if c["cluster_key"].startswith("event:filing:FX-8K-1")]
    assert len(event) == 1
    kinds = {x["data_class"] for x in event[0]["sources"]}
    assert {"FILING_EVENT", "SHORT_INTEREST", "SHORT_SALE_VOLUME", "NEWS_CLAIM"} <= kinds
    pauses = r["payload"]["current_conditions"]["pauses"]
    assert [p["code"] for p in pauses] == ["UNREVIEWED_MATERIAL_EVENT"]          # one development, one pause
    assert r["action"] == "ADD" and r["purchase_eligibility"] == "PAUSED"
    cc = r["payload"]["current_conditions"]
    assert cc["observations"] > cc["independent_developments"]


def test_same_market_risk_is_not_counted_twice(app, demo):
    pf, s = demo["portfolio_id"], sec(demo, "ZZADD")
    add_exposure(app, s, HIGH_RATES, HIGH_REFI)
    app.clock.set(AS_OF + timedelta(hours=1))                     # rates move after the valuation cutoff
    build_market_fixture(app, as_of=app.now(), rates_1m_change=D("0.80"))
    r = get(app, generate(app, pf, s))
    rates = [c for c in r["payload"]["chains"] if c["cluster_key"] == "market:RATES"]
    assert len(rates) == 1 and set(rates[0]["relevance"]["exposures"]) == {"RATES", "REFINANCING"}
    assert len([p for p in r["payload"]["current_conditions"]["pauses"] if p["cluster"] == "market:RATES"]) == 1
    props = r["payload"]["valuation_changes"]["proposals"]
    assert [p["assumption"] for p in props] == ["wacc"]                          # one assumption change, not two
    assert "requires a new valuation version" in props[0]["status"]
    assert r["valuation_version_id"] == get(app, generate(app, pf, s))["valuation_version_id"]   # nothing applied
    lt = portfolio_market_exposure(app, pf, date(2026, 9, 30))
    assert "counted once" in lt["note"] and lt["portfolio_beta_spy"] is not None


# ------------------------------------------------------------------ missing data
def test_missing_data_is_unknown_not_safe(app):
    app.clock.set(AS_OF)
    demo = build_demo(app)
    pf, s = demo["portfolio_id"], sec(demo, "ZZADD")
    add_exposure(app, s, HIGH_RATES)
    snap = build_market_fixture(app, missing=("fred:DGS10", "fred:DGS2"), tag="x")
    assert {"fred:DGS10", "fred:DGS2"} <= set(load_snapshot(app, snap)["missing"])
    r = get(app, generate(app, pf, s))
    assert r["purchase_eligibility"] == "PAUSED"
    assert r["payload"]["current_conditions"]["pauses"][0]["code"] == "UNKNOWN_CONDITION_HIGH_EXPOSURE"
    assert any("UNKNOWN" in m for m in r["payload"]["missing"])


def test_no_approved_exposure_profile_pauses(app):
    app.clock.set(AS_OF)
    d = build_demo(app)
    from equity_monitor.market.exposures import ExposureProfile
    pf, s = d["portfolio_id"], sec(d, "ZZNEW")
    app.conn.execute("DELETE FROM exposure_approval WHERE exposure_version_id IN (SELECT id FROM exposure_profile_version "
                     "WHERE security_id=?)", (s,))
    r = get(app, generate(app, pf, s))
    assert r["action"] == "ADD" and r["purchase_eligibility"] == "PAUSED"
    assert r["payload"]["current_conditions"]["pauses"][0]["code"] == "NO_APPROVED_EXPOSURE_PROFILE"


# ------------------------------------------------------------------ traceability
def test_changed_decisions_are_traceable(app, demo):
    pf, s = demo["portfolio_id"], sec(demo, "ZZADD")
    r1 = get(app, generate(app, pf, s))
    ev = add_exposure(app, s, HIGH_REFI, reason="2027 maturity wall disclosed")
    snap = build_market_fixture(app, hy_level=D("6.5"))
    r2 = get(app, generate(app, pf, s))
    assert r2["id"] != r1["id"] and r2["previous_recommendation_id"] == r1["id"]
    ch = r2["payload"]["changes"]["eligibility"]
    assert ch == {"before": "ELIGIBLE", "now": "PAUSED", "pauses_now": ["ADVERSE_REFINANCING_HIGH_EXPOSURE"]}
    assert r2["market_snapshot_id"] == snap and r2["exposure_version_id"] == ev and r2["policy_version_id"]
    pause = r2["payload"]["current_conditions"]["pauses"][0]
    src = pause["sources"][0]
    assert src["source_id"] == "snapshot" and src["public_at"] and src["observation"].startswith("flag:CREDIT_TIGHTENING")
    snapshot = load_snapshot(app, snap)
    hy = snapshot["indicators"]["fred:BAMLH0A0HYM2"]
    assert hy["source_id"] == "fred" and hy["public_at"]
    with pytest.raises(Exception):
        app.conn.execute("UPDATE market_snapshot SET content_json='{}' WHERE id=?", (snap,))


def test_reviews_share_one_snapshot(app, demo):
    ids = review_portfolio(app, demo["portfolio_id"])
    snaps = {get(app, r)["market_snapshot_id"] for r in ids}
    assert len(snaps) == 1 and None not in snaps


# ------------------------------------------------------------------ interpretation safeguards
def test_interpretation_guards():
    with pytest.raises(InterpretationError, match="NOT equivalent"):
        assert_same_class("SHORT_SALE_VOLUME", "SHORT_INTEREST")
    with pytest.raises(InterpretationError):
        assert_same_class("TRADING_VOLUME", "ETF_FUND_FLOW")
    with pytest.raises(InterpretationError):
        assert_use("SHORT_SALE_VOLUME", "RESEARCH_TRIGGER")
    with pytest.raises(InterpretationError):
        assert_use("OPTIONS_ACTIVITY", "EXPOSURE_TRIGGER")
    for dc, inference in [("IMPLIED_VOL_INDEX", "probability of a business outcome"),
                          ("FUTURES_POSITIONING", "directional forecast"),
                          ("FUTURES_PRICE", "unbiased forecast of the future spot price"),
                          ("OPTIONS_ACTIVITY", "bearish conviction from puts"),
                          ("SHORT_INTEREST", "sell signal"), ("PRICE_RETURN", "causation")]:
        with pytest.raises(InterpretationError):
            assert_inference_allowed(dc, inference)
    assert_use("INTEREST_RATE", "VALUATION_INPUT")


def test_high_short_interest_is_research_not_sell(app, demo):
    pf, s = demo["portfolio_id"], sec(demo, "ZZHLD")
    si = ms.short_interest_spec("ZZHLD")
    ms.store_values(app, si, [(date(2026, 8, 29), D(1_000_000))], raw_id=None)
    ms.store_values(app, si, [(date(2026, 9, 15), D(3_000_000))], raw_id=None, extra={date(2026, 9, 15): {"days_to_cover": "15"}})
    r = get(app, generate(app, pf, s))
    assert r["action"] == "HOLD" and r["purchase_eligibility"] == "ELIGIBLE"
    assert any(x["reason"] == "SHORT_INTEREST_ELEVATED" for x in r["payload"]["current_conditions"]["research"])
    assert app.conn.execute("SELECT COUNT(*) FROM research_task WHERE reason='SHORT_INTEREST_ELEVATED'").fetchone()[0] == 1


def test_market_context_never_bypasses_limits(app, demo):
    pf, s = demo["portfolio_id"], sec(demo, "ZZADD")
    app.policy = Policy(portfolio=PortfolioPolicy(max_issuer_weight=D("0.03"), target_position_weight=D("0.02")))
    build_market_fixture(app, equity_last_month=D("0.20"), oil_3m=D("-0.3"))
    r = get(app, generate(app, pf, s))
    assert r["purchase_eligibility"] == "BLOCKED"
    assert any("issuer weight" in b for b in r["payload"]["current_conditions"]["blocks"])
    assert "ZZADD" not in [l.symbol for l in propose(app, pf).lines if l.amount > 0]


# ------------------------------------------------------------------ exposure profiles
def test_exposure_needs_evidence_or_assumption_label(app, demo):
    with pytest.raises(ValueError, match="duplicate"):
        ExposureProfile(sector=None, sector_benchmark=None, exposures=[HIGH_REFI, HIGH_REFI])
    with pytest.raises(ValueError, match="ANALYST_ASSUMPTION"):
        Exposure(factor="RATES", direction="NEGATIVE", magnitude="HIGH", mechanism="x", basis="EVIDENCED")
    with pytest.raises(ValueError):
        Exposure(factor="REFINANCING", direction="POSITIVE", magnitude="LOW", mechanism="x", basis="ANALYST_ASSUMPTION")
    vid, prof, verification = current_profile(app, sec(demo, "ZZADD"))
    refi = [v for v in verification if v["factor"] == "REFINANCING"][0]
    assert refi["status"] == "VERIFIED"                                        # cited XBRL facts
    assert prof.sector_benchmark == "XLK" and prof.reference_benchmarks == ["SPY", "QQQ"]
    assert all(v["status"] == "ASSUMPTION" for v in verification if v["factor"] != "REFINANCING")


# ------------------------------------------------------------------ point in time & revisions
def test_series_point_in_time_and_revisions(app):
    spec = ms.SPECS["fred:UNRATE"]
    app.clock.set(datetime(2026, 9, 10, 12, tzinfo=UTC))
    ms.store_values(app, spec, [(date(2026, 8, 1), D("4.1"))], raw_id=None)
    first = ms.series_as_of(app, "fred:UNRATE", datetime(2026, 9, 10, 12, tzinfo=UTC))
    assert first[-1].value == D("4.1")
    assert first[-1].public_at == "2026-09-04T12:30:00.000000Z"                # first Friday of Sept 08:30 ET (estimated)
    assert ms.series_as_of(app, "fred:UNRATE", datetime(2026, 9, 4, 12, tzinfo=UTC)) == []   # before release
    app.clock.set(datetime(2026, 10, 3, 12, tzinfo=UTC))
    ms.store_values(app, spec, [(date(2026, 8, 1), D("4.2"))], raw_id=None)   # revision seen later
    ms.store_values(app, spec, [(date(2026, 8, 1), D("4.2"))], raw_id=None)   # unchanged -> no-op
    assert app.conn.execute("SELECT COUNT(*) FROM market_observation WHERE series_key='fred:UNRATE'").fetchone()[0] == 2
    assert ms.series_as_of(app, "fred:UNRATE", datetime(2026, 9, 20, tzinfo=UTC))[-1].value == D("4.1")
    assert ms.series_as_of(app, "fred:UNRATE", datetime(2026, 10, 4, tzinfo=UTC))[-1].value == D("4.2")
    with pytest.raises(Exception):
        app.conn.execute("UPDATE market_observation SET value='1'")


def test_provider_parsers_offline(app):
    rows = ms.parse_fred_csv("observation_date,DGS10\n2026-09-25,4.10\n2026-09-28,.\n")
    assert rows == [(date(2026, 9, 25), D("4.10")), (date(2026, 9, 28), None)]
    out = ms.refresh_fred(app, ["fred:DGS10"], fetch=lambda url: "observation_date,DGS10\n2026-09-25,4.10\n2026-09-28,.\n")
    assert out["fred:DGS10"]["ok"] and out["fred:DGS10"]["new_rows"] == 2
    assert ms.refresh_fred(app, ["fred:DGS10"], fetch=lambda url: "observation_date,DGS10\n2026-09-25,4.10\n2026-09-28,.\n")["fred:DGS10"]["new_rows"] == 0
    assert ms.refresh_fred(app, ["fred:DGS2"], fetch=lambda url: "garbage")["fred:DGS2"]["ok"] is False
    text = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260929|AAA|300|0|1000|B,Q,N\n20260929|BBB|1|0|2|Q\n"
    assert ms.parse_regsho(text, {"AAA"}) == {"AAA": (D(300), D(1000))}
    si = ms.parse_finra_short_interest([{"settlementDate": "2026-09-15", "currentShortPositionQuantity": 128753092,
                                         "daysToCoverQuantity": 2.85, "revisionFlag": None}])
    assert si[0][0] == date(2026, 9, 15) and si[0][1] == D(128753092) and si[0][2]["days_to_cover"] == 2.85
    assert ms.estimated_public_at("CPI", date(2026, 8, 1)).date() == date(2026, 9, 15)
    assert ms.estimated_public_at("FINRA_SI", date(2026, 9, 15)).date() == date(2026, 9, 25)


# ------------------------------------------------------------------ LLM & evaluation
def test_llm_explanations_are_context_only(app, demo):
    pf, s = demo["portfolio_id"], sec(demo, "ZZADD")
    r = get(app, generate(app, pf, s))
    key = r["payload"]["chains"][0]["cluster_key"]
    obs = r["payload"]["chains"][0]["observations"]

    def model(req):
        return {"explanations": [{"hypothesis": "one shared cause", "supporting_observations": obs[:1],
                                  "contradicting_observations": [], "what_would_distinguish": "next filing"}],
                "shared_cause_likely": True, "caveats": "association only"}
    out = explain_cluster(app, FixtureLLM(model), r["id"], key)
    assert out["label"].startswith("INTERPRETATION") and out["explanations"]
    assert generate(app, pf, s) == r["id"]
    bad = explain_cluster(app, FixtureLLM(lambda req: {"explanations": [{"hypothesis": "x", "supporting_observations": ["made-up"],
                                                                        "contradicting_observations": [], "what_would_distinguish": "y"}],
                                                       "shared_cause_likely": False, "caveats": ""}), r["id"], key)
    assert bad["explanations"] is None and "unknown observations" in bad["error"]


def test_paper_variants_for_prospective_comparison(app, demo):
    pf = demo["portfolio_id"]
    add_exposure(app, sec(demo, "ZZADD"), HIGH_REFI)
    build_market_fixture(app, hy_level=D("6.5"))
    review_portfolio(app, pf)
    draft = propose(app, pf)                   # made under a PREVIEW policy: cannot be paper-executed
    app.policy = Policy(status="FROZEN")
    prop = propose(app, pf)                    # re-validated under the FROZEN policy
    books = {}
    for v in ("augmented", "baseline"):
        p = create_portfolio(app, f"paper-{v}", "PAPER")
        a = create_account(app, p, "paper")
        import_csv(app, a, text="date,type,amount\n2026-09-01,DEPOSIT,100000\n")
        books[v] = p
    with pytest.raises(PaperError):
        paper_execute_allocation(app, draft.id, books["augmented"], "augmented")     # policy not frozen at decision
    from equity_monitor.data.prices import Bar, PriceFetch, store_fetch
    for sym in ("ZZADD", "ZZNEW"):
        store_fetch(app, sec(demo, sym), PriceFetch([Bar(date(2026, 10, 1), D("10"), open=D("10"))]), "fixture")
    assert paper_execute_allocation(app, prop.id, books["augmented"], "augmented") == ["ZZNEW"]
    assert sorted(paper_execute_allocation(app, prop.id, books["baseline"], "baseline")) == ["ZZADD", "ZZNEW"]


def test_drafted_refinancing_claim_verifies_with_multiple_debt_facts(app, demo):
    from equity_monitor.research.fundamentals import add_fact
    iss, s = demo["securities"]["ZZHLD"]["issuer_id"], sec(demo, "ZZHLD")
    add_fact(app, iss, "debt_current", 120_000_000, start=None, end=date(2026, 6, 30),
             public_at=datetime(2026, 8, 15, 20, 5, tzinfo=UTC), accession="FIXTURE-CD")
    prof = draft_default_profile(app, s)
    vid = create_profile(app, s, prof, change_reason="with current debt", label="FIXTURE")
    ver = {v["factor"]: v["status"] for v in __import__("json").loads(
        app.conn.execute("SELECT verification_json FROM exposure_profile_version WHERE id=?", (vid,)).fetchone()[0])}
    assert ver["REFINANCING"] == "VERIFIED"
