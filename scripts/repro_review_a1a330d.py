"""FIXTURE reproductions of the review of a1a330d, using only APIs that exist before and after the repair:
P1 an exposure profile approved on a stale VERIFIED snapshot keeps supporting purchases / can be approved;
P2 the LLM budget ignores unknown prices and the next request's cost.
Run: `uv run python scripts/repro_review_a1a330d.py`. Synthetic data only; credential-free (fake paid provider)."""
from decimal import Decimal as D

from equity_monitor.app import memory_app
from equity_monitor.fixtures import AS_OF, build_demo
from equity_monitor.util import Clock


def run(n, f):
    try:
        print(f"[{n}]", f())
    except Exception as e:
        print(f"[{n}] EXC {type(e).__name__}: {str(e)[:160]}")


def _legacy_profile(app, d, sym, claim_text, approve):
    from equity_monitor.data.sec import store_passages
    from equity_monitor.db.core import insert
    from equity_monitor.market.exposures import Exposure, current_profile
    from equity_monitor.research.evidence import Citation, ClaimIn
    from equity_monitor.util import stable_hash, to_json
    sid, iss = d["securities"][sym]["security_id"], d["securities"][sym]["issuer_id"]
    src = "Revenue increased from $3 billion to $4 billion in 2025."
    did = f"doc_{sym}"
    insert(app.conn, "source_document", {"id": did, "provider": "FIXTURE", "doc_type": "10-K", "issuer_id": iss,
        "accession_no": did, "source_url": None, "title": "t", "fiscal_period_end": "2025-12-31", "filed_date": "2026-08-01",
        "public_at": "2026-08-01T21:00:00.000000Z", "public_at_basis": "PROVIDED", "retrieved_at": "2026-08-01T21:00:00.000000Z",
        "raw_object_id": None, "content_hash": None, "parser_version": None, "trust": "FIXTURE", "items": None, "limitations": None})
    store_passages(app, did, src)
    prev_id, prof, _ = current_profile(app, sid)
    prof = prof.with_exposure(Exposure(factor="CONSUMER_SPENDING", direction="POSITIVE", magnitude="LOW", mechanism="demand",
                                       basis="EVIDENCED", evidence=[ClaimIn(text=claim_text, claim_type="FACT",
                                                                            citations=[Citation(passage_id=f"{did}#p0", quote=src)])]))
    ver = [{"factor": e.factor, "status": "ASSUMPTION" if e.basis == "ANALYST_ASSUMPTION" else "VERIFIED", "details": []}
           for e in prof.exposures]                                        # the stale snapshot (no verifier version)
    content = prof.model_dump(mode="json")
    n = app.conn.execute("SELECT MAX(version_no) FROM exposure_profile_version WHERE security_id=?", (sid,)).fetchone()[0]
    vid = f"exv_legacy_{sym}"
    insert(app.conn, "exposure_profile_version", {"id": vid, "security_id": sid, "version_no": n + 1, "prev_version_id": prev_id,
        "content_json": to_json(content), "content_hash": stable_hash(content), "verification_json": to_json(ver),
        "change_reason": "legacy", "author": "USER", "label": "FIXTURE", "evidence_as_of": app.now_iso(), "created_at": app.now_iso()})
    if approve:
        insert(app.conn, "exposure_approval", {"id": f"exa_{sym}", "exposure_version_id": vid, "approved_at": app.now_iso(),
                                               "approver": "owner", "note": ""})
    return sid, vid


def p1():
    from equity_monitor.decisions.recommend import generate, get
    from equity_monitor.market.exposures import approve_profile
    app = memory_app(clock=Clock(AS_OF)); d = build_demo(app)
    bad = "Revenue decreased to $4 billion in 2025."                      # FAILED under the current verifier
    sid, _ = _legacy_profile(app, d, "ZZADD", bad, approve=True)
    r = get(app, generate(app, d["portfolio_id"], sid, force=True))
    _, vid2 = _legacy_profile(app, d, "ZZNEW", bad, approve=False)
    try:
        approve_profile(app, vid2)
        approval = "approved"
    except ValueError as e:
        approval = "refused: " + str(e)[:60]
    return {"approved_legacy_profile_eligibility": r["purchase_eligibility"],
            "pauses": [p["code"] for p in r["payload"]["current_conditions"]["pauses"]], "approve_unapproved_legacy": approval}


class FakePaid:
    name = "anthropic"

    def __init__(self, model):
        self.model, self.sent = model, 0

    def complete(self, req):
        from equity_monitor.llm.base import LLMResponse
        self.sent += 1
        return LLMResponse("anthropic", self.model, '{"ok": true}', "OK", input_tokens=20000, output_tokens=8000)


def p2():
    from equity_monitor.llm.base import LLMRequest
    from equity_monitor.llm.service import call, month_spend
    out = {}
    for label, model, budget in (("unpriced model, $100 budget, 3 calls", "claude-unknown-9", "100"),
                                 ("priced model, $0.01 budget, 2 calls", "claude-opus-5-5", "0.01")):
        app = memory_app(clock=Clock(AS_OF))
        llm = app.settings.llm.model_copy(update={"provider": "anthropic", "monthly_budget_usd": D(budget),
                                                  "use_server_fallbacks": False})
        app.settings = app.settings.model_copy(update={"llm": llm})
        p, errors = FakePaid(model), []
        for _ in range(3 if "3" in label else 2):
            try:
                call(app, p, LLMRequest("t", "v", "s", "x" * 2000, {}, 16000), {})
            except Exception as e:
                errors.append(type(e).__name__)
        out[label] = {"requests_sent": p.sent, "counted_spend": str(month_spend(app)), "refusals": errors}
    return out


for n, f in (("P1", p1), ("P2", p2)):
    run(n, f)
