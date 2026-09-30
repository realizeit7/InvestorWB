"""Owner-entered external research / news claims, verified against primary sources where possible.

A claim becomes a FACT observation only if it is VERIFIED against an ingested passage or XBRL fact of the
same issuer that was public at the claim's publication time. Otherwise it is an INTERPRETATION shown as
context and cannot pause purchases or change a recommendation.
"""

from __future__ import annotations

from datetime import datetime

from ..app import App
from ..db.core import insert, one
from ..research.evidence import VERIFIER_VERSION, Citation, ClaimIn, verify_claim
from ..util import iso_utc, new_id


def add_external_observation(app: App, security_id: str, *, source_name: str, text: str, url: str | None,
                             published_at: datetime, passage_id: str | None = None, quote: str | None = None,
                             fact_id: str | None = None) -> tuple[str, str]:
    issuer = one(app.conn, "SELECT issuer_id FROM security WHERE id=?", (security_id,))["issuer_id"]
    cits = []
    if passage_id or fact_id:
        cits.append(Citation(passage_id=passage_id, fact_id=fact_id, quote=quote))
    claim = ClaimIn(text=text, claim_type="FACT", citations=cits)
    v = verify_claim(app, claim, issuer, app.now()) if issuer else None
    status = v.status if v else "UNVERIFIED"
    cid = new_id("clm")
    insert(app.conn, "claim", {"id": cid, "owner_type": "EXTERNAL", "owner_id": security_id, "text": text,
                               "claim_type": "FACT" if status == "VERIFIED" else "OPINION", "verification": status,
                               "verification_detail": "; ".join(v.details) if v else "no issuer",
                               "citation_status": v.citation_status if v else None,
                               "support_status": v.support_status if v else None, "verifier_version": VERIFIER_VERSION,
                               "created_at": app.now_iso()})
    oid = new_id("ext")
    insert(app.conn, "external_observation", {"id": oid, "security_id": security_id, "sector": None, "level": "COMPANY",
                                              "source_name": source_name, "url": url, "text": text,
                                              "published_at": iso_utc(published_at), "claim_id": cid, "verification": status,
                                              "created_at": app.now_iso()})
    app.audit("market.external_observation", "external_observation", oid, {"verification": status})
    return oid, status
