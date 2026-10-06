"""Structured post-event research records (protocol §10): point-in-time sources, financing facts from XBRL as filed by
the decision cutoff, and quote-verified candidate statements from the event filings. FACT / ASSUMPTION / OPINION are
kept apart; missing values are recorded as None with a reason, never zero.

Deterministic keyword extraction only PROPOSES candidate statements (author DETERMINISTIC_KEYWORD); every quote is
verified verbatim against the stored passage. Judgments (scope of failure, recovery thesis) come only from evidence
packs, labelled RETROSPECTIVE_CONTAMINATED for historical events.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal

from ...app import App
from ...data.http import ProviderError
from ...data.sec import fetch_companyfacts, parse_acceptance
from ...db.core import all_rows, insert, one
from ...util import UTC, iso_utc, new_id, parse_utc
from ..fundamentals import FactView, ingest_companyfacts
from .filings import Fetch, doc_text, store_document

SECTIONS = ("A_FAILURE", "B_REMAINING_BUSINESS", "C_FINANCING", "D_RECOVERY")
MIN_REVENUE_FOR_DCF = Decimal("50000000")   # below this the operating-company DCF is not applied (pre-revenue biotech)
# Records-quality rule (docs/selloff/IMPLEMENTATION_NOTES.md; NOT a cohort rule): a value whose period ended more than
# this many days before the cutoff is stale — e.g. a debt tag the company stopped using years earlier — and is unknown.
STALE_DAYS = 400


def add_fact(app: App, event_id: str, section: str, field: str, kind: str, *, value_text: str | None = None,
             value_num: Decimal | None = None, unit: str | None = None, period: str | None = None,
             null_reason: str | None = None, source_kind: str = "NONE", fact_ref: str | None = None,
             passage_id: str | None = None, quote: str | None = None, verification_status: str = "NOT_REQUIRED",
             author: str = "DETERMINISTIC", label: str | None = None, note: str | None = None) -> str:
    if section not in SECTIONS or kind not in ("FACT", "ASSUMPTION", "OPINION"):
        raise ValueError(f"bad section/kind {section}/{kind}")
    if kind == "FACT" and value_text is None and value_num is None and not null_reason:
        raise ValueError("a FACT without a value needs a null_reason (missing is never zero)")
    fid = new_id("srf")
    insert(app.conn, "sr_fact", {"id": fid, "event_id": event_id, "section": section, "field": field, "kind": kind,
                                 "value_text": value_text, "value_num": None if value_num is None else str(value_num),
                                 "unit": unit, "period": period, "null_reason": null_reason, "source_kind": source_kind,
                                 "fact_ref": fact_ref, "passage_id": passage_id, "quote": quote,
                                 "verification_status": verification_status, "verifier_version": None,
                                 "author": author, "label": label, "note": note, "created_at": app.now_iso()})
    return fid


# ------------------------------------------------------------------ point-in-time source manifest
def attach_periodic_reports(app: App, event: dict, sub: dict, fetch: Fetch | None) -> dict:
    """Latest 10-Q and 10-K accepted BY the cutoff (fetched, used); the next periodic report AFTER the cutoff is
    recorded as excluded (metadata only), so the boundary is visible in the manifest."""
    cutoff = parse_utc(event["decision_cutoff"])
    periodic = [f for f in sub["filings"] if f["form"] in ("10-Q", "10-K", "10-Q/A", "10-K/A") and f.get("acceptanceDateTime")]
    out = {"used": [], "excluded": []}
    for form in ("10-Q", "10-K"):
        before = sorted([f for f in periodic if f["form"] == form and parse_acceptance(f["acceptanceDateTime"]) <= cutoff],
                        key=lambda f: f["acceptanceDateTime"])
        if not before:
            insert(app.conn, "sr_gap", {"id": new_id("srg"), "event_id": event["id"], "kind": "MISSING_PERIODIC_REPORT",
                                        "description": f"no {form} accepted before the cutoff", "created_at": app.now_iso()})
            continue
        f = before[-1]
        if fetch is None:
            continue
        did = store_document(app, fetch, cik=event["cik"], issuer_id=event["issuer_id"], accession=f["accessionNumber"],
                             file_name=f["primaryDocument"], doc_type=f["form"],
                             accepted=parse_acceptance(f["acceptanceDateTime"]), filing_date=f["filingDate"],
                             report_date=f.get("reportDate"))
        insert(app.conn, "sr_source", {"id": new_id("srsrc"), "event_id": event["id"], "document_id": did,
                                       "role": f"LATEST_{form.replace('-', '')}",
                                       "public_at": iso_utc(parse_acceptance(f["acceptanceDateTime"])),
                                       "excluded_reason": None, "created_at": app.now_iso()}, or_ignore=True)
        out["used"].append(f["accessionNumber"])
    after = sorted([f for f in periodic if parse_acceptance(f["acceptanceDateTime"]) > cutoff],
                   key=lambda f: f["acceptanceDateTime"])[:1]
    for f in after:
        out["excluded"].append(f["accessionNumber"])
        insert(app.conn, "sr_gap", {"id": new_id("srg"), "event_id": event["id"], "kind": "EXCLUDED_AFTER_CUTOFF",
                                    "description": f"{f['form']} {f['accessionNumber']} accepted {f['acceptanceDateTime']} "
                                                   "is after the decision cutoff: not used", "created_at": app.now_iso()})
    return out


def pit_documents(app: App, event: dict) -> list[dict]:
    """Every stored document of this event's issuer that was public by the cutoff (later ones are never returned)."""
    return [dict(r) for r in all_rows(app.conn, "SELECT * FROM source_document WHERE issuer_id=? AND public_at<=? "
                                                "ORDER BY public_at DESC", (event["issuer_id"], event["decision_cutoff"]))]


# ------------------------------------------------------------------ C: financing (XBRL as filed by the cutoff)
def ingest_financials(app: App, event: dict, fetch_facts=None) -> dict:
    if one(app.conn, "SELECT 1 FROM financial_fact WHERE issuer_id=? LIMIT 1", (event["issuer_id"],)):
        return {"ingested": False}
    try:
        if fetch_facts is None:
            from ...data.sec import make_client
            client = make_client(app)
            fetch_facts = lambda iid: fetch_companyfacts(app, client, iid)  # noqa: E731
        raw, rid = fetch_facts(event["issuer_id"])
    except ProviderError as exc:
        insert(app.conn, "sr_gap", {"id": new_id("srg"), "event_id": event["id"], "kind": "XBRL_UNAVAILABLE",
                                    "description": str(exc)[:300], "created_at": app.now_iso()})
        return {"ingested": False, "error": str(exc)}
    return {"ingested": True, **ingest_companyfacts(app, event["issuer_id"], raw, rid)}


def _later_revised(app: App, issuer_id: str, fv_value, concept: str) -> bool:
    if fv_value is None:
        return False
    later = FactView(app, issuer_id, datetime(2100, 1, 1, tzinfo=UTC))
    v2 = [f for f in later._facts.get(concept, []) if f.end == fv_value.end and f.start == fv_value.start]
    return any(f.value != fv_value.value for f in v2)


def financing_record(app: App, event: dict) -> dict:
    """Cash, investments, debt, revenue, operating cash flow and a STATED-formula historical cash-use runway."""
    cutoff = parse_utc(event["decision_cutoff"])
    fv = FactView(app, event["issuer_id"], cutoff)
    eid = event["id"]
    out: dict = {}

    def put(section, field, fval, unit="USD"):
        if fval is not None and fval.value is not None and (cutoff.date() - fval.end).days > STALE_DAYS:
            add_fact(app, eid, section, field, "FACT", source_kind="NONE", verification_status="STALE",
                     null_reason=(f"unknown: the latest value filed by the cutoff is for a period ending {fval.end} "
                                  f"(> {STALE_DAYS} days earlier; accession {fval.accession}) — not treated as current"))
            out[field] = None
            return None
        if fval is None or fval.value is None:
            add_fact(app, eid, section, field, "FACT", null_reason="unknown: not found under the shared XBRL concept map in filings public by the cutoff (may be reported under another tag)",
                     source_kind="NONE", verification_status="MISSING")
            out[field] = None
            return None
        lab = "LATER_REVISED (value as filed by the cutoff is used)" if _later_revised(app, event["issuer_id"], fval, fval.concept) else None
        add_fact(app, eid, section, field, "FACT", value_num=fval.value, unit=unit,
                 period=f"{fval.start or ''}..{fval.end}", source_kind="XBRL", fact_ref=fval.fact_id,
                 verification_status="XBRL_AS_FILED_BY_CUTOFF", label=lab,
                 note=f"accession {fval.accession}, public {fval.public_at}")
        out[field] = fval.value
        return fval

    cash = put("C_FINANCING", "cash_and_equivalents", fv.instant("cash"))
    sti = put("C_FINANCING", "short_term_investments", fv.instant("short_term_investments"))
    add_fact(app, eid, "C_FINANCING", "long_term_investments", "FACT", source_kind="NONE", verification_status="MISSING",
             null_reason="not captured by the shared concept map (noncurrent marketable securities): unknown, not zero")
    debt = None
    for c in ("long_term_debt_total", "long_term_debt_noncurrent"):
        debt = fv.instant(c)
        if debt is not None:
            break
    put("C_FINANCING", "long_term_debt", debt)
    put("C_FINANCING", "current_debt", fv.instant("debt_current") or fv.instant("long_term_debt_current"))
    put("C_FINANCING", "shares_outstanding", fv.instant("shares_outstanding"), unit="shares")
    fy_rev = (fv.annual("revenue") or [None])[-1]
    put("B_REMAINING_BUSINESS", "revenue_latest_fiscal_year", fy_rev)
    fy_cfo = (fv.annual("cfo") or [None])[-1]
    put("C_FINANCING", "operating_cash_flow_latest_fiscal_year", fy_cfo)
    q_cfo = fv.quarters("cfo")
    last4 = q_cfo[-4:] if len(q_cfo) >= 4 else []
    ttm = sum((q.value for q in last4), Decimal(0)) if last4 and all(q.value is not None for q in last4) else None
    if ttm is not None:
        add_fact(app, eid, "C_FINANCING", "operating_cash_flow_trailing_4_quarters", "FACT", value_num=ttm, unit="USD",
                 period=f"{last4[0].start}..{last4[-1].end}", source_kind="DERIVED",
                 fact_ref=",".join(q.fact_id or "" for q in last4), verification_status="DERIVED_FROM_XBRL",
                 note="sum of four discrete quarters (YTD differences where needed)")
    burn_basis = (ttm, f"trailing 4 quarters to {last4[-1].end}") if ttm is not None else \
        ((fy_cfo.value, f"fiscal year to {fy_cfo.end}") if fy_cfo is not None else (None, None))
    liquid = None if cash is None else cash.value + (sti.value if sti is not None else Decimal(0))
    if liquid is None or burn_basis[0] is None:
        add_fact(app, eid, "C_FINANCING", "historical_cash_use_runway_months", "ASSUMPTION", source_kind="NONE",
                 verification_status="NOT_COMPUTABLE",
                 null_reason="cash or operating cash flow unknown at the cutoff", note="no runway is assumed")
    elif burn_basis[0] >= 0:
        add_fact(app, eid, "C_FINANCING", "historical_cash_use_runway_months", "ASSUMPTION",
                 value_text="not burning cash on this basis", source_kind="DERIVED", verification_status="DERIVED_FROM_XBRL",
                 note=f"operating cash flow {burn_basis[0]:,.0f} over the {burn_basis[1]}")
    else:
        months = (liquid / (-burn_basis[0]) * 12).quantize(Decimal("0.1"))
        add_fact(app, eid, "C_FINANCING", "historical_cash_use_runway_months", "ASSUMPTION", value_num=months,
                 unit="months", period=burn_basis[1], source_kind="DERIVED", verification_status="DERIVED_FROM_XBRL",
                 note=("formula: (cash + short-term investments) / (-operating cash flow) x 12 using the "
                       f"{burn_basis[1]}; long-term investments unknown (excluded); historical cash use is NOT a "
                       "forecast — it ignores restructuring after the failure, milestones, financing and debt maturities"
                       + ("" if sti is not None else "; short-term investments not reported (treated as unknown, excluded)")))
    rev = fy_rev.value if fy_rev is not None else None
    add_fact(app, eid, "D_RECOVERY", "valuation_method", "ASSUMPTION",
             value_text=("UNSUPPORTED_VALUATION: pre-revenue or small-revenue company — the operating-company DCF is not "
                         "applied; prior price highs or averages are never fair value") if rev is None or rev < MIN_REVENUE_FOR_DCF
             else "not valued in sr-0.1 (feasibility only); prior price highs or averages are never fair value",
             source_kind="NONE", verification_status="NOT_REQUIRED")
    return {k: (None if v is None else str(v)) for k, v in out.items()}


# ------------------------------------------------------------------ A/B/D: quote-verified candidate statements
_CANDIDATES = {
    "A_FAILURE": [("program_status", r"\b(discontinu\w*|will not (?:file|pursue|advance)|no longer|terminat\w*|wind down|"
                                     r"deprioritiz\w*|evaluate (?:next steps|strategic|the path)|strategic (?:options|alternatives)|"
                                     r"(?:continue|plan) to (?:analyze|evaluate|advance|discuss))\b"),
                  ("safety", r"\b(well tolerated|safety profile|serious adverse|adverse events?)\b")],
    "B_REMAINING_BUSINESS": [("pipeline", r"\bPhase\s*(?:1|2|3|I{1,3})\b"),
                             ("partnerships", r"\b(collaborat\w+|licens\w+ agreement|partner\w*)\b")],
    "C_FINANCING": [("cash_statement", r"\b(cash runway|cash, cash equivalents|fund (?:operations|its operations))\b")],
    "D_RECOVERY": [("catalysts", r"\b(expect\w*|anticipat\w*|plan\w*)\b[^.]{0,80}\b(20\d\d|quarter|half|year-end)\b")],
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z•])", re.sub(r"\s+", " ", text)) if 40 <= len(s.strip()) <= 500]


def candidate_statements(app: App, event: dict, max_per_field: int = 3) -> int:
    """Keyword-proposed sentences from the event filings, each quote verified verbatim (FACT = the company said it)."""
    docs = [r["document_id"] for r in all_rows(app.conn, "SELECT DISTINCT document_id FROM sr_source WHERE event_id=? AND "
                                                         "role IN ('EVENT_FILING','EVENT_EXHIBIT')", (event["id"],))]
    n = 0
    seen: set[str] = set()
    for section, fields in _CANDIDATES.items():
        for field, rx in fields:
            got = 0
            for did in docs:
                for p in all_rows(app.conn, "SELECT id, text FROM document_passage WHERE document_id=? ORDER BY ordinal", (did,)):
                    for s in _sentences(p["text"]):
                        if got >= max_per_field or s in seen or not re.search(rx, s, re.I):
                            continue
                        if "forward-looking" in s.lower() or "risks and uncertainties" in s.lower():
                            continue
                        seen.add(s)
                        verified = re.sub(r"\s+", " ", s) in re.sub(r"\s+", " ", p["text"])
                        add_fact(app, event["id"], section, field, "FACT", value_text=s, source_kind="PASSAGE",
                                 passage_id=p["id"], quote=s,
                                 verification_status="QUOTE_VERIFIED" if verified else "QUOTE_NOT_FOUND",
                                 author="DETERMINISTIC_KEYWORD", label="CANDIDATE: human review required",
                                 note="the company made this statement; its truth or relevance is not assessed")
                        got += 1
                        n += 1
    scr = one(app.conn, "SELECT quote, quote_passage_id, quote_verified FROM sr_screen WHERE id=?", (event["screen_id"],))
    add_fact(app, event["id"], "A_FAILURE", "stated_failure", "FACT", value_text=scr["quote"], source_kind="PASSAGE",
             passage_id=scr["quote_passage_id"], quote=scr["quote"],
             verification_status="QUOTE_VERIFIED" if scr["quote_verified"] else "QUOTE_NOT_FOUND", author="SCREENING")
    return n + 1
