"""Citation and numeric-claim verification.

A FACT claim is VERIFIED only if every citation
  - references an existing passage (or normalized fact) of the SAME issuer,
  - whose document/fact was public at or before ``as_of``,
  - and, for passages, the quoted text appears verbatim (case/whitespace-insensitive);
and every number in the claim text appears in a cited quote or matches a cited fact value.

Forged or unsupported citations yield FAILED; missing citations yield UNVERIFIED. Only VERIFIED
facts may feed actionable decisions. ASSUMPTION/OPINION claims are labelled, never verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from ..app import App
from ..db.core import one
from ..util import iso_utc


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passage_id: str | None = None
    fact_id: str | None = None
    quote: str | None = Field(default=None, max_length=2000)


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=2000)
    claim_type: str = Field(pattern="^(FACT|ASSUMPTION|OPINION)$")
    citations: list[Citation] = Field(default_factory=list, max_length=10)
    supports: bool = True


@dataclass
class Verification:
    status: str                      # VERIFIED | UNVERIFIED | FAILED | NOT_REQUIRED
    details: list[str] = field(default_factory=list)
    valid_citations: list[dict] = field(default_factory=list)


_WS = re.compile(r"\s+")
_NUM = re.compile(r"(?<![\w.])[-(]?\$?\d[\d,]*(?:\.\d+)?%?\)?(?:\s*(?:billion|million|thousand|bn|mm|m|k)\b)?", re.I)
SCALE = {"billion": Decimal(10) ** 9, "bn": Decimal(10) ** 9, "million": Decimal(10) ** 6, "mm": Decimal(10) ** 6,
         "m": Decimal(10) ** 6, "thousand": Decimal(1000), "k": Decimal(1000)}


def norm_text(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-")
    return _WS.sub(" ", s).strip().lower()


def extract_numbers(text: str) -> list[tuple[str, Decimal]]:
    out = []
    for m in _NUM.finditer(text):
        tok = m.group(0)
        core = re.sub(r"[^\d.]", "", tok.split()[0] if " " in tok else re.sub(r"[a-zA-Z]+$", "", tok))
        if not core or core == ".":
            continue
        try:
            v = Decimal(core)
        except InvalidOperation:
            continue
        low = tok.lower()
        for word, mult in SCALE.items():
            if re.search(rf"\b{word}\b", low):
                v *= mult
                break
        # skip bare years and single digits (item numbers, list markers); they are not quantitative claims
        if "%" not in tok and "$" not in tok and not any(w in low for w in SCALE) and (v < 10 or 1900 <= v <= 2100):
            continue
        out.append((tok.strip(), v))
    return out


def _number_supported(tok: str, value: Decimal, quotes: list[str], fact_values: list[Decimal]) -> bool:
    digits = re.sub(r"[^\d.]", "", tok.split()[0])
    for q in quotes:
        if digits and digits in re.sub(r"[,$]", "", q):
            return True
        for _t, qv in extract_numbers(q):
            if qv and abs(qv - value) <= abs(qv) * Decimal("0.005"):
                return True
    for fv in fact_values:
        if fv and abs(fv - value) <= abs(fv) * Decimal("0.005"):
            return True
    return False


def verify_claim(app: App, claim: ClaimIn, issuer_id: str, as_of: datetime) -> Verification:
    if claim.claim_type != "FACT":
        return Verification("NOT_REQUIRED", ["labelled as " + claim.claim_type])
    if not claim.citations:
        return Verification("UNVERIFIED", ["no citation provided"])
    cutoff = iso_utc(as_of)
    details, valid, quotes, facts = [], [], [], []
    failed = False
    for c in claim.citations:
        if c.passage_id:
            p = one(app.conn, "SELECT p.text, d.issuer_id, d.public_at, d.id AS doc_id FROM document_passage p "
                              "JOIN source_document d ON d.id=p.document_id WHERE p.id=?", (c.passage_id,))
            if p is None:
                failed = True
                details.append(f"passage {c.passage_id} does not exist")
                continue
            if p["issuer_id"] != issuer_id:
                failed = True
                details.append(f"passage {c.passage_id} belongs to another issuer")
                continue
            if p["public_at"] is None or p["public_at"] > cutoff:
                failed = True
                details.append(f"passage {c.passage_id} was not public as of {cutoff}")
                continue
            if not c.quote or norm_text(c.quote) not in norm_text(p["text"]):
                failed = True
                details.append(f"quote not found verbatim in {c.passage_id}")
                continue
            quotes.append(c.quote)
            valid.append({"passage_id": c.passage_id, "document_id": p["doc_id"], "quote": c.quote})
        elif c.fact_id:
            f = one(app.conn, "SELECT issuer_id, public_at, value FROM financial_fact WHERE id=?", (c.fact_id,))
            if f is None or f["issuer_id"] != issuer_id:
                failed = True
                details.append(f"fact {c.fact_id} does not exist for this issuer")
                continue
            if f["public_at"] > cutoff:
                failed = True
                details.append(f"fact {c.fact_id} was not public as of {cutoff}")
                continue
            if f["value"] is not None:
                facts.append(Decimal(f["value"]))
            valid.append({"fact_id": c.fact_id})
        else:
            failed = True
            details.append("citation has neither passage_id nor fact_id")
    if failed:
        return Verification("FAILED", details, valid)
    for tok, v in extract_numbers(claim.text):
        if not _number_supported(tok, v, quotes, facts):
            return Verification("FAILED", details + [f"number '{tok}' is not supported by the cited evidence"], valid)
    return Verification("VERIFIED", details, valid)
