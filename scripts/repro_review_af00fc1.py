"""FIXTURE reproductions of the follow-up review findings R1-R3 (review of af00fc1).
Run: `uv run python scripts/repro_review_af00fc1.py`. Outputs before/after are recorded in VALIDATION.md §6.
Synthetic data only; not market evidence."""
from datetime import date
from decimal import Decimal as D
from equity_monitor.app import memory_app
from equity_monitor.util import Clock
from equity_monitor.config.models import Policy
from equity_monitor.fixtures import build_demo, AS_OF

def run(n, f):
    try: print(f"[{n}]", f())
    except Exception as e: print(f"[{n}] EXC {type(e).__name__}: {e}")

def r1():
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
    text = "Revenue increased from $3 billion to $4 billion in 2025."
    store_passages(app, "d1", text)
    out = {}
    for c in ["Revenue increased from $4 billion to $3 billion in 2025.", "Revenue was $3 billion in 2025.",
              "Revenue increased from $3 billion to $4 billion in 2025.", "Revenue was $4 billion in 2025."]:
        out[c] = verify_claim(app, ClaimIn(text=c, claim_type="FACT", citations=[Citation(passage_id="d1#p0", quote=text)]), iss, AS_OF).status
    return out

def _paper(app, name, csv):
    from equity_monitor.ledger.store import create_portfolio, create_account
    from equity_monitor.ledger.csv_import import import_csv
    p = create_portfolio(app, name, "PAPER"); a = create_account(app, p, "p")
    import_csv(app, a, text="date,type,symbol,quantity,price,amount\n" + csv)
    return p

def r2():
    from equity_monitor.decisions.allocation import propose
    from equity_monitor.data.prices import store_fetch, PriceFetch, Bar
    from equity_monitor.evaluation.paper import paper_execute_allocation
    from equity_monitor.ledger.views import portfolio_view
    app = memory_app(clock=Clock(AS_OF)); d = build_demo(app)
    app.policy = Policy(status="FROZEN")
    p1, p2 = propose(app, d["portfolio_id"]), propose(app, d["portfolio_id"])
    pp = _paper(app, "paper", "2026-09-01,DEPOSIT,,,,3000\n")
    for s in ("ZZADD", "ZZNEW"):
        store_fetch(app, d["securities"][s]["security_id"], PriceFetch([Bar(date(2026,10,1), D(10), open=D(10))]), "fixture")
    a = paper_execute_allocation(app, p1.id, pp, "augmented")
    try:
        b = paper_execute_allocation(app, p2.id, pp, "augmented")
    except Exception as e:      # repaired behaviour: one allocation per paper book per session
        b = f"{type(e).__name__}: mutually exclusive" if "mutually exclusive" in str(e) else repr(e)
    v = portfolio_view(app, pp, date(2026,10,1))
    return {"first": a, "second": b, "issuer_weights": {k[:10]: f"{w:.3%}" for k, w in v.issuer_weights.items()}, "cash": str(v.cash)}

def r3():
    from equity_monitor.decisions.recommend import generate
    from equity_monitor.data.prices import store_fetch, PriceFetch, Bar
    from equity_monitor.evaluation.paper import paper_execute
    from equity_monitor.ledger.views import portfolio_view
    app = memory_app(clock=Clock(AS_OF)); d = build_demo(app)
    app.policy = Policy(status="FROZEN")
    sid = d["securities"]["ZZTRM"]["security_id"]
    rid = generate(app, d["portfolio_id"], sid)
    books = {n: _paper(app, n, "2026-09-01,DEPOSIT,,,,6000\n2026-09-02,BUY,ZZTRM,100,40,\n") for n in ("augmented", "baseline")}
    store_fetch(app, sid, PriceFetch([Bar(date(2026,10,1), D(40), open=D(40))]), "fixture")
    res = {n: paper_execute(app, rid, p) for n, p in books.items()}
    return {n: (bool(res[n]), str(portfolio_view(app, p, date(2026,10,1)).holding(sid).shares)) for n, p in books.items()}

for n, f in (("R1", r1), ("R2", r2), ("R3", r3)):
    run(n, f)
