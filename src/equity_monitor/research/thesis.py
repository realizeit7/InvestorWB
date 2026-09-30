"""Versioned investment theses.

- Every version is immutable (DB trigger). A change is a new version with a reason and the
  evidence cutoff at the time it was written.
- The ORIGINAL thesis is the first approved version and is always shown next to the current one,
  so a later rewrite cannot silently replace the reason the position was opened.
- Drafts (including LLM drafts) have no effect until the owner approves them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ..app import App
from ..db.core import all_rows, insert, one, transaction
from ..util import D, dstr, from_json, iso_utc, new_id, stable_hash, to_json
from .evidence import VERIFIER_VERSION, ClaimIn, verify_claim

COMPARATORS = (">=", "<=", ">", "<")


class MilestoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(max_length=1000)
    concept: str | None = None          # canonical fact concept for deterministic checks
    comparator: str | None = Field(default=None, pattern="^(>=|<=|>|<)$")
    target: Decimal | None = None       # for ratios use decimals (0.25 = 25%)
    metric: str | None = Field(default=None, pattern="^(value|margin|growth_yoy)$")
    due_date: date | None = None


class ConditionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(max_length=1000)
    kind: str = Field(pattern="^(METRIC|EVENT|JUDGMENT)$")
    concept: str | None = None
    metric: str | None = Field(default=None, pattern="^(value|margin|growth_yoy)$")
    comparator: str | None = Field(default=None, pattern="^(>=|<=|>|<)$")
    threshold: Decimal | None = None
    consecutive_periods: int | None = Field(default=None, ge=1, le=8)
    period_basis: str | None = Field(default="FY", pattern="^(FY|TTM)$")


class ThesisContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_model: str = Field(max_length=4000)
    valuation_requires: str = Field(max_length=4000)
    our_view_differs: str = Field(max_length=4000)
    evidence: list[ClaimIn] = Field(default_factory=list, max_length=40)
    counterargument: str = Field(max_length=4000)
    milestones: list[MilestoneIn] = Field(default_factory=list, max_length=20)
    invalidation_conditions: list[ConditionIn] = Field(min_length=1, max_length=20)
    value_realization: str = Field(max_length=2000)
    key_risks: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    next_review_date: date


@dataclass
class ThesisVersion:
    id: str
    thesis_id: str
    security_id: str
    version_no: int
    content: dict
    change_reason: str
    author: str
    created_at: str
    evidence_as_of: str
    approved_at: str | None
    label: str
    claims: list[dict]
    milestones: list[dict]
    conditions: list[dict]

    @property
    def approved(self) -> bool:
        return self.approved_at is not None


def _thesis_id(app: App, security_id: str) -> str:
    r = one(app.conn, "SELECT id FROM thesis WHERE security_id=?", (security_id,))
    if r:
        return r["id"]
    tid = new_id("th")
    insert(app.conn, "thesis", {"id": tid, "security_id": security_id, "created_at": app.now_iso()})
    return tid


def create_version(app: App, security_id: str, content: ThesisContent, *, change_reason: str, author: str = "USER",
                   llm_call_id: str | None = None, label: str = "ACTUAL", as_of: datetime | None = None) -> str:
    """Store a new (unapproved) thesis version. Claims are verified against evidence public at ``as_of``."""
    as_of = as_of or app.now()
    issuer_id = one(app.conn, "SELECT issuer_id FROM security WHERE id=?", (security_id,))["issuer_id"]
    tid = _thesis_id(app, security_id)
    prev = one(app.conn, "SELECT id, version_no FROM thesis_version WHERE thesis_id=? ORDER BY version_no DESC LIMIT 1", (tid,))
    if prev and not change_reason.strip():
        raise ValueError("a change reason is required for every new thesis version")
    evid = [dict(r) for r in all_rows(app.conn, "SELECT id, doc_type, accession_no, public_at FROM source_document "
                                                "WHERE issuer_id=? AND public_at<=? ORDER BY public_at DESC LIMIT 50",
                                      (issuer_id, iso_utc(as_of)))]
    latest_fact = one(app.conn, "SELECT MAX(public_at) AS p FROM financial_fact WHERE issuer_id=? AND public_at<=?",
                      (issuer_id, iso_utc(as_of)))["p"]
    vid = new_id("thv")
    cjson = content.model_dump(mode="json")
    with transaction(app.conn):
        insert(app.conn, "thesis_version", {
            "id": vid, "thesis_id": tid, "version_no": (prev["version_no"] + 1) if prev else 1,
            "prev_version_id": prev["id"] if prev else None, "content_json": to_json(cjson),
            "content_hash": stable_hash(cjson), "change_reason": change_reason or "initial thesis",
            "evidence_as_of": iso_utc(as_of), "evidence_json": to_json({"documents": evid, "latest_fact_public_at": latest_fact}),
            "author": author, "llm_call_id": llm_call_id, "label": label, "created_at": app.now_iso(),
        })
        for c in content.evidence:
            v = verify_claim(app, c, issuer_id, as_of)
            cid = new_id("clm")
            insert(app.conn, "claim", {"id": cid, "owner_type": "THESIS_VERSION", "owner_id": vid, "text": c.text,
                                       "claim_type": c.claim_type, "verification": v.status,
                                       "verification_detail": "; ".join(v.details) or None,
                                       "citation_status": v.citation_status, "support_status": v.support_status,
                                       "verifier_version": VERIFIER_VERSION, "created_at": app.now_iso()})
            for cit in c.citations:
                ok = any((vc.get("passage_id") == cit.passage_id and cit.passage_id) or
                         (vc.get("fact_id") == cit.fact_id and cit.fact_id) for vc in v.valid_citations)
                exists_p = cit.passage_id and one(app.conn, "SELECT 1 FROM document_passage WHERE id=?", (cit.passage_id,))
                exists_f = cit.fact_id and one(app.conn, "SELECT 1 FROM financial_fact WHERE id=?", (cit.fact_id,))
                insert(app.conn, "evidence_link", {
                    "id": new_id("evl"), "claim_id": cid,
                    "document_id": one(app.conn, "SELECT document_id FROM document_passage WHERE id=?", (cit.passage_id,))["document_id"] if exists_p else None,
                    "passage_id": cit.passage_id if exists_p else None, "fact_id": cit.fact_id if exists_f else None,
                    "quote": cit.quote if exists_p or exists_f else f"[unresolved reference] {cit.passage_id or cit.fact_id}: {cit.quote}",
                    "supports": int(c.supports), "verified": int(ok and v.status == "VERIFIED"),
                })
        for m in content.milestones:
            insert(app.conn, "milestone", {"id": new_id("ms"), "thesis_version_id": vid, "description": m.description,
                                           "concept": _metric_key(m.concept, m.metric), "comparator": m.comparator,
                                           "target": dstr(m.target), "due_date": m.due_date.isoformat() if m.due_date else None})
        for c in content.invalidation_conditions:
            insert(app.conn, "invalidation_condition", {
                "id": new_id("ic"), "thesis_version_id": vid, "description": c.description, "kind": c.kind,
                "concept": _metric_key(c.concept, c.metric), "comparator": c.comparator, "threshold": dstr(c.threshold),
                "consecutive_periods": c.consecutive_periods, "period_basis": c.period_basis})
    app.audit("thesis.version_created", "thesis_version", vid, {"security_id": security_id, "author": author})
    return vid


def _metric_key(concept: str | None, metric: str | None) -> str | None:
    if concept is None:
        return None
    return f"{concept}:{metric or 'value'}"


class ThesisEvidenceError(ValueError):
    """A thesis cannot be approved while its FACT claims fail or are not substantively verified (unless acknowledged)."""


def approve_version(app: App, version_id: str, approver: str = "owner", note: str = "",
                    acknowledge_unverified: bool = False) -> None:
    """Approve a thesis version. FACT claims that FAILED (broken citation, contradicted or unsupported) block approval:
    correct them in a new version. FACT claims that are only SOURCE_MATCHED (citation intact, content not checked) or
    UNVERIFIED need ``acknowledge_unverified=True``; the acknowledged claims are recorded in the approval note."""
    if one(app.conn, "SELECT 1 FROM thesis_approval WHERE thesis_version_id=?", (version_id,)):
        return
    claims = all_rows(app.conn, "SELECT text, verification FROM claim WHERE owner_type='THESIS_VERSION' AND owner_id=? "
                                "AND claim_type='FACT'", (version_id,))
    failed = [c["text"] for c in claims if c["verification"] == "FAILED"]
    if failed:
        raise ThesisEvidenceError("FACT claims failed verification; create a corrected version: " + " | ".join(failed))
    weak = [f"[{c['verification']}] {c['text']}" for c in claims if c["verification"] in ("SOURCE_MATCHED", "UNVERIFIED")]
    if weak and not acknowledge_unverified:
        raise ThesisEvidenceError("FACT claims are not substantively verified (review them, then pass "
                                  "acknowledge_unverified=True / --acknowledge-unverified): " + " | ".join(weak))
    if weak:
        note = (note + " | " if note else "") + "acknowledged unverified claims: " + " | ".join(weak)
    insert(app.conn, "thesis_approval", {"id": new_id("tap"), "thesis_version_id": version_id,
                                         "approved_at": app.now_iso(), "approver": approver, "note": note})
    app.audit("thesis.approved", "thesis_version", version_id, {"note": note})


def _load(app: App, r) -> ThesisVersion:
    ap = one(app.conn, "SELECT approved_at FROM thesis_approval WHERE thesis_version_id=?", (r["id"],))
    sec = one(app.conn, "SELECT security_id FROM thesis WHERE id=?", (r["thesis_id"],))["security_id"]
    claims = []
    for c in all_rows(app.conn, "SELECT * FROM claim WHERE owner_type='THESIS_VERSION' AND owner_id=?", (r["id"],)):
        links = [dict(l) for l in all_rows(app.conn, "SELECT * FROM evidence_link WHERE claim_id=?", (c["id"],))]
        claims.append({**dict(c), "links": links})
    return ThesisVersion(
        id=r["id"], thesis_id=r["thesis_id"], security_id=sec, version_no=r["version_no"],
        content=from_json(r["content_json"]), change_reason=r["change_reason"], author=r["author"],
        created_at=r["created_at"], evidence_as_of=r["evidence_as_of"], approved_at=ap["approved_at"] if ap else None,
        label=r["label"], claims=claims,
        milestones=[dict(m) for m in all_rows(app.conn, "SELECT * FROM milestone WHERE thesis_version_id=?", (r["id"],))],
        conditions=[dict(c) for c in all_rows(app.conn, "SELECT * FROM invalidation_condition WHERE thesis_version_id=?", (r["id"],))],
    )


def history(app: App, security_id: str) -> list[ThesisVersion]:
    return [_load(app, r) for r in all_rows(app.conn, "SELECT v.* FROM thesis_version v JOIN thesis t ON t.id=v.thesis_id "
                                                      "WHERE t.security_id=? ORDER BY v.version_no", (security_id,))]


def current_version(app: App, security_id: str, as_of: str | None = None) -> ThesisVersion | None:
    """Latest version approved at or before ``as_of``."""
    sql = ("SELECT v.* FROM thesis_version v JOIN thesis t ON t.id=v.thesis_id JOIN thesis_approval a "
           "ON a.thesis_version_id=v.id WHERE t.security_id=?")
    params: list = [security_id]
    if as_of:
        sql += " AND a.approved_at<=?"
        params.append(as_of)
    r = one(app.conn, sql + " ORDER BY v.version_no DESC LIMIT 1", params)
    return _load(app, r) if r else None


def original_version(app: App, security_id: str) -> ThesisVersion | None:
    r = one(app.conn, "SELECT v.* FROM thesis_version v JOIN thesis t ON t.id=v.thesis_id JOIN thesis_approval a "
                      "ON a.thesis_version_id=v.id WHERE t.security_id=? ORDER BY v.version_no LIMIT 1", (security_id,))
    return _load(app, r) if r else None


def record_assessment(app: App, subject_type: str, subject_id: str, state: str, *, verified: bool, assessor: str,
                      evidence: dict | None = None, as_of: datetime | None = None) -> str:
    if assessor == "LLM" and verified and state == "TRIGGERED":
        # An LLM can surface a possible trigger but cannot by itself verify an invalidation.
        verified = False
    aid = new_id("cas")
    insert(app.conn, "condition_assessment", {
        "id": aid, "subject_type": subject_type, "subject_id": subject_id, "as_of": iso_utc(as_of or app.now()),
        "state": state, "verified": int(verified), "assessor": assessor, "evidence_json": to_json(evidence or {}),
        "created_at": app.now_iso(),
    })
    app.audit("thesis.assessment", subject_type.lower(), subject_id, {"state": state, "verified": verified, "by": assessor})
    return aid


def latest_assessment(app: App, subject_type: str, subject_id: str, as_of: str | None = None) -> dict | None:
    sql = "SELECT * FROM condition_assessment WHERE subject_type=? AND subject_id=?"
    params: list = [subject_type, subject_id]
    if as_of:
        sql += " AND as_of<=?"
        params.append(as_of)
    r = one(app.conn, sql + " ORDER BY as_of DESC, created_at DESC, rowid DESC LIMIT 1", params)
    return dict(r) if r else None
