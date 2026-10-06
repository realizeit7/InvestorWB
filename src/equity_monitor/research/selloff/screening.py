"""Document-based screening in the protocol's fixed order, then event construction with deduplication.

Every screened accession gets a recorded decision (INCLUDED / EXCLUDED / UNRESOLVED) with its category, the reason and
a quote that must appear VERBATIM in that filing's stored documents (whitespace-normalized). Price-based criteria are
not used here (protocol §5). Events are built only from INCLUDED decisions, grouped by event key (CIK + trial
identifier): the earliest filing gives the timing; the others are recorded as merged accessions (duplicates).
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from ...app import App
from ...db.core import all_rows, insert, one
from ...data.sec import parse_acceptance
from ...util import new_id, parse_utc
from .discovery import pool
from .filings import Fetch, doc_text, fetch_filing, parse_cover, store_document, submissions
from .home import load_protocol, protocol_hash
from .timing import event_timing

CATEGORIES = ("PRIMARY_ENDPOINT_FAILURE", "FUTILITY_STOP", "SAFETY_STOP", "COMMERCIAL_DISCONTINUATION",
              "REGULATORY_REJECTION", "MIXED_OR_UNCLEAR", "NOT_AN_EVENT", "OUT_OF_SCOPE")
COHORT_PHASES = ("Phase 2", "Phase 2b", "Phase 2/3", "Phase 3")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("“", '"').replace("”", '"')).strip()


def find_quote(app: App, accession: str, quote: str) -> str | None:
    """Passage id containing ``quote`` verbatim (whitespace/quote-normalized) in any stored document of the filing."""
    q = _norm(quote)
    if not q:
        return None
    for d in all_rows(app.conn, "SELECT id FROM source_document WHERE accession_no=?", (accession,)):
        for p in all_rows(app.conn, "SELECT id, text FROM document_passage WHERE document_id=? ORDER BY ordinal", (d["id"],)):
            if q in _norm(p["text"]):
                return p["id"]
        if q in _norm(doc_text(app, d["id"])):                 # quote spanning a passage boundary
            return f"{d['id']}#doc"
    return None


def latest_decisions(app: App) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in all_rows(app.conn, "SELECT * FROM sr_screen ORDER BY created_at, rowid"):
        out[r["accession"]] = dict(r) | {"details": json.loads(r["details_json"])}
    return out


def queue(app: App, n: int, fetch: Fetch) -> list[dict]:
    """The next ``n`` unscreened accessions in protocol order, with their filings fetched (for a human/LLM screener)."""
    done = latest_decisions(app)
    out = []
    for r in pool(app):
        if r["accession"] in done:
            continue
        f = fetch_filing(app, r["accession"], r["cik"], [x for x in (r["files"] or "").split(",") if x], fetch)
        out.append(r | {"filing": f})
        if len(out) >= n:
            break
    return out


_MATCH = re.compile(r"did not meet (?:the|its) primary endpoint|failed to meet (?:the|its) primary endpoint|did not achieve "
                    r"(?:the|its) primary endpoint|primary endpoint was not met|did not achieve statistical significance on "
                    r"the primary endpoint", re.I)
_PHASE = re.compile(r"Phase\s*(?:1/2|2/3|1b|2a|2b|3|III|II|I|1|2)\b[^.]{0,80}", re.I)


def describe(app: App, item: dict) -> str:
    """Plain-text view of a queued filing for the screener: cover, timing sources, headline, matches, phase mentions."""
    f = item["filing"]
    cov = json.loads(f["cover_json"])
    t = doc_text(app, f["hit_document_id"]) if f["hit_document_id"] else ""
    lines = [f"#### rank {item['sample_rank']}  {item['accession']}  CIK {item['cik']}  filed {item['file_date']}",
             f"  accepted {f['accepted_at']} | dateline {f['dateline_date']} | report date {cov.get('report_date')}",
             f"  cover (at the time): {cov.get('company')} {cov.get('symbols')} {cov.get('exchanges')} | "
             f"EDGAR today: {(item.get('display_name') or '')[:60]}",
             "  HEAD: " + _norm(t[:350])]
    for m in list(_MATCH.finditer(t))[:2]:
        lines.append("  >> " + _norm(t[max(0, m.start() - 450): m.end() + 250]))
    ph = sorted({_norm(m.group(0))[:60] for m in _PHASE.finditer(t)})[:4]
    lines.append(f"  phase mentions: {ph}")
    return "\n".join(lines)


def status(app: App) -> dict:
    proto, _h, _t = load_protocol()
    s = proto["sampling"]
    d = latest_decisions(app)
    inc = [x for x in d.values() if x["decision"] == "INCLUDED"]
    keys = {x["details"].get("event_key") for x in inc}
    screened = len(d)
    stop = len(keys) >= s["screen_until_included"] or screened >= s["max_screened"]
    return {"screened": screened, "included_filings": len(inc), "distinct_events": len(keys),
            "stop_rule_met": stop, "target": s["screen_until_included"], "max_screened": s["max_screened"],
            "minimum_events": s["minimum_events"],
            "by_category": {c: sum(1 for x in d.values() if x["category"] == c) for c in CATEGORIES}}


def event_key(cik: str, details: dict) -> str:
    tid = (details.get("trial_id") or "").strip().upper()
    if not tid:
        tid = "|".join(_norm(details.get(k) or "").lower() for k in ("drug", "indication", "phase"))
    return f"{int(cik)}|{tid}"


def record(app: App, decisions: list[dict], screener: str, fetch: Fetch | None = None) -> list[dict]:
    """Append screening decisions. INCLUDED needs category PRIMARY_ENDPOINT_FAILURE, a cohort phase, drug, indication,
    and quotes (failure + phase) found verbatim in the filing; otherwise the decision is stored as UNRESOLVED."""
    ph = protocol_hash(app)
    order = {r["accession"]: r for r in pool(app)}
    out = []
    for dec in decisions:
        acc = dec["accession"]
        if acc not in order:
            raise ValueError(f"{acc} is not in the protocol's discovery pool")
        if dec["category"] not in CATEGORIES:
            raise ValueError(f"unknown category {dec['category']}")
        if fetch is not None and not one(app.conn, "SELECT 1 FROM sr_filing WHERE accession=?", (acc,)):
            r = order[acc]
            fetch_filing(app, acc, r["cik"], [x for x in (r["files"] or "").split(",") if x], fetch)
        details = {k: dec.get(k) for k in ("drug", "indication", "trial_id", "trial_id_source", "partner_run", "flags",
                                           "phase_quote", "occurred_date", "occurred_quote", "trial_name")}
        pid = find_quote(app, acc, dec.get("quote") or "")
        decision, reason = dec["decision"], dec["reason"]
        problems = []
        if dec.get("quote") and pid is None:
            problems.append("quote not found verbatim in the filing")
        if decision == "INCLUDED":
            if dec["category"] != "PRIMARY_ENDPOINT_FAILURE":
                problems.append(f"category {dec['category']} is not in the sr-0.1 cohort")
            if dec.get("phase") not in COHORT_PHASES:
                problems.append(f"phase {dec.get('phase')!r} is not a cohort phase")
            if not (dec.get("drug") and dec.get("indication")):
                problems.append("drug and indication are required")
            if not dec.get("quote"):
                problems.append("a failure quote is required")
            if dec.get("phase_quote") and find_quote(app, acc, dec["phase_quote"]) is None:
                problems.append("phase quote not found verbatim")
            details["phase_quote_verified"] = bool(dec.get("phase_quote")) and \
                find_quote(app, acc, dec["phase_quote"]) is not None
            details["phase"] = dec.get("phase")
            details["event_key"] = event_key(order[acc]["cik"], details)
        if problems:
            reason = f"{reason} [UNRESOLVED: {'; '.join(problems)}]"
            decision = "UNRESOLVED"
        row = {"id": new_id("srsc"), "accession": acc, "sample_rank": order[acc]["sample_rank"],
               "sample_key": order[acc]["sample_key"], "decision": decision, "category": dec["category"],
               "phase": dec.get("phase"), "reason": reason, "quote": dec.get("quote"), "quote_passage_id": pid,
               "quote_verified": int(pid is not None), "details_json": json.dumps(details), "screener": screener,
               "protocol_hash": ph, "created_at": app.now_iso()}
        insert(app.conn, "sr_screen", row)
        out.append(row)
    return out


# ------------------------------------------------------------------ events
def _ticker_at_time(app: App, f: dict, cutoff: datetime, fetch: Fetch | None, sub: dict | None) -> tuple[dict, list[str]]:
    """Ticker/exchange as stated on the event 8-K cover; else the latest 10-Q/10-K cover filed by the cutoff."""
    cover = json.loads(f["cover_json"])
    gaps = []
    if cover.get("symbols"):
        return {"ticker": cover["symbols"][0], "exchange": (cover.get("exchanges") or [None])[0],
                "source": f"8-K cover {f['accession']}", "all_symbols": cover["symbols"]}, gaps
    if fetch is not None and sub is not None:
        for r in sorted([x for x in sub["filings"] if x["form"] in ("10-Q", "10-K") and x.get("acceptanceDateTime")
                         and parse_acceptance(x["acceptanceDateTime"]) <= cutoff],
                        key=lambda x: x["acceptanceDateTime"], reverse=True)[:2]:
            did = store_document(app, fetch, cik=f["cik"], issuer_id=f["issuer_id"], accession=r["accessionNumber"],
                                 file_name=r["primaryDocument"], doc_type=r["form"],
                                 accepted=parse_acceptance(r["acceptanceDateTime"]), filing_date=r["filingDate"],
                                 report_date=r.get("reportDate"))
            c2 = parse_cover(doc_text(app, did))
            if c2.get("symbols"):
                return {"ticker": c2["symbols"][0], "exchange": (c2.get("exchanges") or [None])[0],
                        "source": f"{r['form']} cover {r['accessionNumber']} (filed before the cutoff)",
                        "all_symbols": c2["symbols"]}, gaps
    from .filings import release_ticker
    for did in (f["hit_document_id"], f["main_document_id"]):
        if did:
            tk, ex = release_ticker(doc_text(app, did))
            if tk:
                return {"ticker": tk, "exchange": ex, "source": f"press release text in {f['accession']} (e.g. '(Nasdaq: XXX)')",
                        "all_symbols": [tk]}, gaps
    gaps.append("ticker at the time not stated on the event 8-K cover, in the release, or on a periodic report filed by the cutoff")
    return {"ticker": None, "exchange": None, "source": None, "all_symbols": []}, gaps


def build_events(app: App, fetch: Fetch | None = None) -> dict:
    """Create events from INCLUDED decisions (idempotent by event key). Timing from the earliest merged filing."""
    ph = protocol_hash(app)
    groups: dict[str, list[dict]] = {}
    for d in latest_decisions(app).values():
        if d["decision"] == "INCLUDED":
            groups.setdefault(d["details"]["event_key"], []).append(d)
    created, existing = 0, 0
    for key, decs in groups.items():
        if one(app.conn, "SELECT 1 FROM sr_event WHERE event_key=?", (key,)):
            existing += 1
            continue
        fil = [dict(one(app.conn, "SELECT * FROM sr_filing WHERE accession=?", (d["accession"],))) for d in decs]
        timings = []
        for f in fil:
            cover = json.loads(f["cover_json"])
            t = event_timing(accepted=parse_utc(f["accepted_at"]) if f["accepted_at"] else None,
                             filing_date=f["filing_date"], dateline=f["dateline_date"], report_date=cover.get("report_date"))
            timings.append((t.public_earliest, t, f, next(d for d in decs if d["accession"] == f["accession"])))
        timings.sort(key=lambda x: x[0])
        _e, t, f, dec = timings[0]
        sub = submissions(app, f["cik"], fetch) if fetch is not None and not json.loads(f["cover_json"]).get("symbols") else None
        tick, gaps = _ticker_at_time(app, f, t.decision_cutoff, fetch, sub)
        det = dec["details"]
        prev_same_drug = one(app.conn, "SELECT id FROM sr_event WHERE cik=? AND lower(drug)=lower(?) AND event_key<>? "
                                       "ORDER BY public_earliest LIMIT 1", (str(int(f["cik"])), det.get("drug") or "", key))
        eid = new_id("sre")
        flags = list(det.get("flags") or [])
        if det.get("partner_run"):
            flags.append("PARTNER_RUN_TRIAL")
        if not det.get("phase_quote_verified"):
            flags.append("PHASE_NOT_QUOTE_VERIFIED")
        insert(app.conn, "sr_event", {
            "id": eid, "event_key": key, "cik": str(int(f["cik"])), "issuer_id": f["issuer_id"], "security_id": None,
            "company_name_at_time": json.loads(f["cover_json"]).get("company"), "ticker_at_time": tick["ticker"],
            "exchange_at_time": tick["exchange"], "security_class": "common stock (first class on the cover page)",
            "drug": det.get("drug"), "indication": det.get("indication"),
            "trial_id": det.get("trial_id") or det.get("trial_name"),
            "trial_id_source": det.get("trial_id_source") or ("TRIAL_NAME" if det.get("trial_name") else None),
            "phase": dec["phase"], "category": dec["category"],
            "flags_json": json.dumps(flags + (["TICKER_SOURCE: " + tick["source"]] if tick["source"] else []) +
                                     (["MULTIPLE_SYMBOLS: " + ",".join(tick["all_symbols"])] if len(tick["all_symbols"]) > 1 else [])),
            "occurred_date": det.get("occurred_date"),
            "occurred_basis": "stated in the filing" if det.get("occurred_date") else "not stated",
            "submitted_at": f["accepted_at"], "public_earliest": t.public_earliest.isoformat(),
            "public_latest": t.public_latest.isoformat(), "public_precision": t.precision, "public_basis": t.basis,
            "retrieved_at": f["retrieved_at"], "pre_session": t.pre_session.isoformat(),
            "measurement_session": t.measurement_session.isoformat(), "decision_cutoff": t.decision_cutoff.isoformat(),
            "entry_session": t.entry_session.isoformat(), "timing_notes": "; ".join(t.notes) or None,
            "accessions_json": json.dumps([x[2]["accession"] for x in timings]),
            "repeat_of": prev_same_drug["id"] if prev_same_drug else None, "screen_id": dec["id"],
            "protocol_hash": ph, "created_at": app.now_iso()})
        for g in gaps:
            insert(app.conn, "sr_gap", {"id": new_id("srg"), "event_id": eid, "kind": "TICKER_AT_TIME", "description": g,
                                        "created_at": app.now_iso()})
        for _e2, _t2, f2, _d2 in timings:
            for role, did in (("EVENT_FILING", f2["main_document_id"]), ("EVENT_EXHIBIT", f2["hit_document_id"])):
                if did:
                    insert(app.conn, "sr_source", {"id": new_id("srsrc"), "event_id": eid, "document_id": did, "role": role,
                                                   "public_at": one(app.conn, "SELECT public_at FROM source_document WHERE id=?",
                                                                    (did,))["public_at"], "excluded_reason": None,
                                                   "created_at": app.now_iso()}, or_ignore=True)
        created += 1
    return {"created": created, "existing": existing, "events": len(groups)}
