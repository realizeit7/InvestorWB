"""FIXTURE reproductions of the review of be46212: P1a (direction on a level) and P1b (downgraded evidence keeps its
approval). Run: `uv run python scripts/repro_review_be46212.py`. Before/after outputs are in VALIDATION.md §7.
Synthetic data only; not market evidence."""
from equity_monitor.app import memory_app
from equity_monitor.util import Clock
from equity_monitor.fixtures import build_demo, AS_OF


def run(n, f):
    try:
        print(f"[{n}]", f())
    except Exception as e:
        print(f"[{n}] EXC {type(e).__name__}: {e}")


def _doc(app, iss, text, doc_id, public_at="2026-08-01T21:00:00.000000Z"):
    from equity_monitor.db.core import insert
    from equity_monitor.data.sec import store_passages
    insert(app.conn, "source_document", {"id": doc_id, "provider": "FIXTURE", "doc_type": "10-K", "issuer_id": iss,
        "accession_no": doc_id, "source_url": None, "title": "t", "fiscal_period_end": "2025-12-31", "filed_date": public_at[:10],
        "public_at": public_at, "public_at_basis": "PROVIDED", "retrieved_at": public_at, "raw_object_id": None,
        "content_hash": None, "parser_version": None, "trust": "FIXTURE", "items": None, "limitations": None})
    store_passages(app, doc_id, text)
    return f"{doc_id}#p0"


def p1a():
    from equity_monitor.data.securities import get_or_create_issuer
    from equity_monitor.research.evidence import ClaimIn, Citation, verify_claim
    app = memory_app(clock=Clock(AS_OF))
    iss = get_or_create_issuer(app.conn, app.now_iso(), name="Co", cik="1")
    up = "Revenue increased from $3 billion to $4 billion in 2025."
    down = "Revenue decreased from $5 billion to $4 billion in 2025."
    pu, pd = _doc(app, iss, up, "up"), _doc(app, iss, down, "down")
    out = {}
    for src, pid, claim in ((up, pu, "Revenue decreased to $4 billion in 2025."), (down, pd, "Revenue increased to $4 billion in 2025."),
                            (up, pu, "Revenue increased to $4 billion in 2025."), (up, pu, "Revenue was $4 billion in 2025.")):
        out[f"{pid[:-3]}: {claim}"] = verify_claim(app, ClaimIn(text=claim, claim_type="FACT",
                                                               citations=[Citation(passage_id=pid, quote=src)]), iss, AS_OF).status
    return out


def p1b(tmp=None):
    import shutil
    from equity_monitor.db import core
    from equity_monitor.decisions.recommend import generate, get
    from equity_monitor.research.evidence import Citation, ClaimIn
    from equity_monitor.research.thesis import ThesisContent, approve_version, create_version, current_version
    import tempfile
    tmp = tmp or tempfile.mkdtemp(prefix="eqm_p1b_")
    orig = core._migration_files
    core._migration_files = lambda: [f for f in orig() if f[0] != "0007_claim_reverify.sql"]
    try:
        from pathlib import Path
        app = memory_app(clock=Clock(AS_OF), home=Path(tmp))
        d = build_demo(app)
        sid, iss = d["securities"]["ZZADD"]["security_id"], d["securities"]["ZZADD"]["issuer_id"]
        src = "Revenue increased from $3 billion to $4 billion in 2025."
        pid = _doc(app, iss, src, "legacy")
        base = current_version(app, sid).content
        claim = ClaimIn(text="Revenue increased from $4 billion to $3 billion in 2025.", claim_type="FACT",
                        citations=[Citation(passage_id=pid, quote=src)])
        vid = create_version(app, sid, ThesisContent.model_validate({**base, "evidence": [claim.model_dump()]}),
                             change_reason="legacy", as_of=AS_OF)
        # simulate the ev-2 verdict on that claim, then a normal approval (no acknowledgement needed for VERIFIED)
        app.conn.execute("UPDATE claim SET verification='VERIFIED', support_status='CONFIRMED', verifier_version='ev-2', "
                         "citation_status='SOURCE_MATCHED' WHERE owner_id=?", (vid,))
        app.conn.execute("UPDATE evidence_link SET verified=1 WHERE claim_id IN (SELECT id FROM claim WHERE owner_id=?)", (vid,))
        # the approval as the pre-repair code recorded it (VERIFIED claims needed no acknowledgement)
        app.conn.execute("INSERT INTO thesis_approval(id, thesis_version_id, approved_at, approver, note) "
                         "VALUES ('tap_legacy', ?, ?, 'owner', '')", (vid, app.now_iso()))
    finally:
        core._migration_files = orig
    applied = core.migrate(app.conn)
    c = app.conn.execute("SELECT verification, support_status FROM claim WHERE owner_id=?", (vid,)).fetchone()
    note = app.conn.execute("SELECT note FROM thesis_approval WHERE thesis_version_id=?", (vid,)).fetchone()[0]
    r = get(app, generate(app, d["portfolio_id"], sid, force=True))
    try:
        approve_version(app, vid)
        reapprove = "returned without requiring review"
    except Exception as e:
        reapprove = f"{type(e).__name__}"
    return {"applied": applied, "claim": tuple(c), "approved_version_current": current_version(app, sid).id == vid,
            "approval_note": note, "new_rec": (r["action"], r["purchase_eligibility"]), "approve_again": reapprove}


for n, f in (("P1a", p1a), ("P1b", p1b)):
    run(n, f)
