"""FIXTURE reproductions of review findings 1-7 (review of a2d0ef8). Run: `uv run python scripts/repro_review_a2d0ef8.py`.

Outputs on a2d0ef8 and after the repair are recorded in VALIDATION.md §5. [4] now raises by design: the old flow froze the
policy only after the decision; [4b] is the same scenario done correctly. Synthetic data only; not market evidence.
"""
import json, traceback
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from equity_monitor.app import memory_app
from equity_monitor.util import Clock
from equity_monitor.config.models import Policy, MarketPolicy
from equity_monitor.fixtures import build_demo, AS_OF
UTC = timezone.utc

def fresh():
    app = memory_app(clock=Clock(AS_OF)); d = build_demo(app); return app, d

def run(name, fn):
    try: print(f"[{name}]", fn())
    except Exception as e: print(f"[{name}] EXC {type(e).__name__}: {e}")

# 1 citation verification
def f1():
    from equity_monitor.data.securities import get_or_create_issuer
    from equity_monitor.db.core import insert
    from equity_monitor.data.sec import store_passages
    from equity_monitor.research.evidence import ClaimIn, Citation, verify_claim
    app = memory_app(clock=Clock(AS_OF))
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="Co", cik="1")
    insert(app.conn, "source_document", {"id": "d1", "provider": "FIXTURE", "doc_type": "10-K", "issuer_id": iss, "accession_no": "a1",
        "source_url": None, "title": "t", "fiscal_period_end": "2025-12-31", "filed_date": "2026-02-01", "public_at": "2026-02-01T21:00:00.000000Z",
        "public_at_basis": "PROVIDED", "retrieved_at": "2026-02-01T21:00:00.000000Z", "raw_object_id": None, "content_hash": None,
        "parser_version": None, "trust": "FIXTURE", "items": None, "limitations": None})
    text = "Revenue increased 12% to $4.2 billion. The company has no material debt."
    store_passages(app, "d1", text)
    out = {}
    for c in ["Revenue decreased 12% to $4.2 billion.", "Revenue increased 12% to $4.2 trillion.", "The company is insolvent."]:
        out[c] = verify_claim(app, ClaimIn(text=c, claim_type="FACT", citations=[Citation(passage_id="d1#p0", quote=text)]), iss, AS_OF).status
    return out

# 2 share classes
def f2():
    from equity_monitor.data.securities import register_security
    from equity_monitor.data.prices import store_fetch, PriceFetch, Bar
    from equity_monitor.data import calendar as cal
    from equity_monitor.research.thesis import create_version, approve_version, current_version, ThesisContent
    from equity_monitor.valuation.store import latest_valuation, create_valuation, approve_valuation
    from equity_monitor.valuation.dcf import ScenarioInputs
    from equity_monitor.market.exposures import current_profile, create_profile, approve_profile
    from equity_monitor.decisions.recommend import set_watchlist, review_portfolio
    from equity_monitor.decisions.allocation import propose
    from equity_monitor.ledger.views import portfolio_view
    app, d = fresh()
    app.policy = Policy(market=MarketPolicy(enabled=False))
    a = d["securities"]["ZZADD"]
    b = register_security(app.conn, app.now_iso(), "ZZADD.B", security_type="COMMON", issuer_id=a["issuer_id"])
    bars = app.conn.execute("SELECT session_date, close FROM price_bar WHERE security_id=?", (a["security_id"],)).fetchall()
    store_fetch(app, b, PriceFetch([Bar(date.fromisoformat(r["session_date"]), D(r["close"])) for r in bars]), "fixture")
    tv = current_version(app, a["security_id"])
    v = create_version(app, b, ThesisContent.model_validate(tv.content), change_reason="class B", author="FIXTURE", label="FIXTURE", as_of=AS_OF)
    approve_version(app, v)
    val = latest_valuation(app, a["security_id"])
    vid = create_valuation(app, b, {k: ScenarioInputs.model_validate(val.inputs[k]) for k in ("bear","base","bull")}, evidence_as_of=AS_OF, label="FIXTURE")
    approve_valuation(app, vid, downside_reviewed=True)
    approve_profile(app, create_profile(app, b, current_profile(app, a["security_id"])[1], change_reason="x", label="FIXTURE"))
    app.conn.execute("INSERT INTO source_check(id,provider,subject,check_type,checked_at,success,latest_seen,error,job_run_id) VALUES('c','fixture',?, 'FILINGS', ?,1,NULL,NULL,NULL)", (a["issuer_id"], app.now_iso()))
    set_watchlist(app, b, "APPROVED")
    review_portfolio(app, d["portfolio_id"])
    p = propose(app, d["portfolio_id"])
    view = portfolio_view(app, d["portfolio_id"])
    lines = {l.symbol: l.amount for l in p.lines if l.amount > 0}
    cur = view.issuer_weights[a["issuer_id"]] * view.nav
    comb = (cur + lines.get("ZZADD", 0) + lines.get("ZZADD.B", 0)) / p.nav_after
    return {"lines": {k: str(v) for k, v in lines.items()}, "combined_issuer_weight": f"{comb:.4%}",
            "baseline": [(x["symbol"], str(x["amount"])) for x in p.baseline["lines"]]}

# 3 stale recommendations
def f3():
    from equity_monitor.decisions.recommend import review_portfolio, generate, get
    from equity_monitor.decisions.allocation import propose
    app, d = fresh()
    review_portfolio(app, d["portfolio_id"])
    app.clock.set(AS_OF + timedelta(days=2))
    p = propose(app, d["portfolio_id"])
    fresh_rec = get(app, generate(app, d["portfolio_id"], d["securities"]["ZZADD"]["security_id"]))
    return {"proposed": {l.symbol: str(l.amount) for l in p.lines if l.amount > 0}, "fresh_ZZADD": (fresh_rec["action"], fresh_rec["reason_codes"])}

# 4 paper
def f4():
    from equity_monitor.decisions.recommend import review_portfolio, generate
    from equity_monitor.decisions.allocation import propose
    from equity_monitor.ledger.store import create_portfolio, create_account
    from equity_monitor.ledger.views import portfolio_view
    from equity_monitor.evaluation.paper import paper_execute_allocation, paper_execute
    from equity_monitor.data.prices import store_fetch, PriceFetch, Bar
    app, d = fresh()
    review_portfolio(app, d["portfolio_id"]); p = propose(app, d["portfolio_id"])
    pp = create_portfolio(app, "paper", "PAPER"); create_account(app, pp, "p")
    for s in ("ZZADD", "ZZNEW"):
        store_fetch(app, d["securities"][s]["security_id"], PriceFetch([Bar(date(2026,10,1), D(10), open=D(10))]), "fixture")
    app.policy = Policy(status="FROZEN")
    a = paper_execute_allocation(app, p.id, pp, "augmented")
    cashA = portfolio_view(app, pp, date(2026,10,1)).cash
    # B: PAUSED ADD executed
    app2 = memory_app(clock=Clock(AS_OF)); d2 = build_demo(app2)
    s = d2["securities"]["ZZNEW"]["security_id"]
    app2.conn.execute("DELETE FROM exposure_approval WHERE exposure_version_id IN (SELECT id FROM exposure_profile_version WHERE security_id=?)", (s,))
    rid = generate(app2, d2["portfolio_id"], s)
    elig = app2.conn.execute("SELECT action, purchase_eligibility FROM recommendation WHERE id=?", (rid,)).fetchone()
    pp2 = create_portfolio(app2, "paper", "PAPER"); create_account(app2, pp2, "p")
    store_fetch(app2, s, PriceFetch([Bar(date(2026,10,1), D(10), open=D(10))]), "fixture")
    app2.policy = Policy(status="FROZEN")
    pid = paper_execute(app2, rid, pp2)
    return {"A_bought": a, "A_cash": str(cashA), "B_rec": tuple(elig), "B_fill": pid, "B_cash": str(portfolio_view(app2, pp2, date(2026,10,1)).cash)}

# 5 debt
def f5():
    from equity_monitor.data.securities import get_or_create_issuer
    from equity_monitor.research.fundamentals import add_fact, FactView
    app = memory_app(clock=Clock(AS_OF))
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="Co", cik="5")
    pub = datetime(2026, 2, 1, 21, tzinfo=UTC)
    try:
        add_fact(app, iss, "current_debt", 100, start=None, end=date(2025,12,31), public_at=pub, accession="A", tag="LongTermDebtCurrent")
        add_fact(app, iss, "current_debt", 300, start=None, end=date(2025,12,31), public_at=pub, accession="A", tag="DebtCurrent")
        rows = app.conn.execute("SELECT source_tag, value FROM financial_fact").fetchall()
        return {"stored": [tuple(r) for r in rows], "selected": str(FactView(app, iss, AS_OF).instant("current_debt").value)}
    except KeyError:   # repaired normalizer: separate concepts, combined without double counting
        from equity_monitor.research.fundamentals import debt_total
        for c, v in (("long_term_debt_noncurrent", 800), ("long_term_debt_current", 100), ("debt_current", 300)):
            add_fact(app, iss, c, v, start=None, end=date(2025,12,31), public_at=pub, accession="A")
        agg = debt_total(FactView(app, iss, AS_OF))
        return {"debt_total": str(agg.value), "method": agg.method, "complete": agg.complete}

# 6 subperiod benchmark
def f6():
    from equity_monitor.evaluation.performance import contribution_matched
    from equity_monitor.ledger.views import portfolio_view
    app, d = fresh()
    b = contribution_matched(app, d["portfolio_id"], "SCHG", date(2026,9,1), date(2026,9,30))
    return {"nav": str(portfolio_view(app, d["portfolio_id"], date(2026,9,30)).nav), "bench_end": str(b.values[-1][1]) if b.values else None, "unapplied": b.unapplied}

# 7 eligibility-change alert
def f7():
    from equity_monitor.monitoring import scheduler as sch
    from equity_monitor.monitoring.jobs import JobContext, handlers
    from equity_monitor.data.prices import FixturePriceProvider, PriceFetch, Bar
    from equity_monitor.decisions.recommend import latest_for
    from equity_monitor.fixtures import build_market_fixture
    from equity_monitor.market.exposures import current_profile, create_profile, approve_profile, Exposure
    app, d = fresh()
    build_market_fixture(app)
    data = {s: PriceFetch([Bar(date(2026,9,30), v.get("price") or D(30))]) for s, v in d["securities"].items()}
    prov = FixturePriceProvider(data)
    ctx = JobContext(price_provider=prov, refresh_market_series=False)
    spec = sch.DEFAULT_JOBS[0]
    sch.run_instance(app, spec, sch.latest_due(spec, app.now()), handlers(ctx)["daily_refresh"])
    s = d["securities"]["ZZADD"]["security_id"]
    before = latest_for(app, d["portfolio_id"], s)
    prof = current_profile(app, s)[1].with_exposure(Exposure(factor="REFINANCING", direction="NEGATIVE", magnitude="HIGH", mechanism="x", basis="ANALYST_ASSUMPTION"))
    approve_profile(app, create_profile(app, s, prof, change_reason="x", label="FIXTURE"))
    app.clock.set(AS_OF + timedelta(hours=1))
    build_market_fixture(app, as_of=app.now(), hy_level=D("6.5"))
    sch.run_instance(app, spec, sch.latest_due(spec, app.now()), handlers(ctx)["daily_refresh"], force=True)
    after = latest_for(app, d["portfolio_id"], s)
    alerts = [r["title"] for r in app.conn.execute("SELECT title FROM alert WHERE kind='MATERIAL_EVENT'")]
    return {"before": (before["action"], before["purchase_eligibility"]), "after": (after["action"], after["purchase_eligibility"]), "new_rec": before["id"] != after["id"], "alerts": alerts}

for n, f in [("1", f1), ("2", f2), ("3", f3), ("4", f4), ("5", f5), ("6", f6), ("7", f7)]:
    run(n, f)

# 4b: the same scenario with the policy frozen BEFORE the decision (the only way paper execution is allowed now)
def f4b():
    from equity_monitor.decisions.allocation import propose
    from equity_monitor.ledger.store import create_portfolio, create_account
    from equity_monitor.ledger.views import portfolio_view
    from equity_monitor.evaluation.paper import paper_execute_allocation, paper_execute, PaperError
    from equity_monitor.data.prices import store_fetch, PriceFetch, Bar
    from equity_monitor.decisions.recommend import generate
    app, d = fresh()
    app.policy = Policy(status="FROZEN")
    p = propose(app, d["portfolio_id"])
    pp = create_portfolio(app, "paper", "PAPER"); create_account(app, pp, "p")
    for s in ("ZZADD", "ZZNEW"):
        store_fetch(app, d["securities"][s]["security_id"], PriceFetch([Bar(date(2026,10,1), D(10), open=D(10))]), "fixture")
    a = paper_execute_allocation(app, p.id, pp, "augmented")
    s = d["securities"]["ZZNEW"]["security_id"]
    app.conn.execute("DELETE FROM exposure_approval WHERE exposure_version_id IN (SELECT id FROM exposure_profile_version WHERE security_id=?)", (s,))
    rid = generate(app, d["portfolio_id"], s)
    try:
        b = paper_execute(app, rid, pp)
    except PaperError as e:
        b = f"PaperError: {str(e)[:70]}"
    return {"A_unfunded_bought": a, "A_cash": str(portfolio_view(app, pp, date(2026,10,1)).cash), "B_paused_add_direct": b}

run("4b", f4b)
