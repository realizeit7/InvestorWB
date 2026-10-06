"""Coverage table and funnel for the feasibility report — built only from stored records; never computes returns."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter

from ...app import App
from ...db.core import all_rows, one
from .discovery import pool, sensitivity_gap
from .screening import latest_decisions, status

KEY_FINANCIALS = ("cash_and_equivalents", "short_term_investments", "long_term_debt", "current_debt", "shares_outstanding",
                  "revenue_latest_fiscal_year", "operating_cash_flow_latest_fiscal_year")


def _latest(app: App, table: str, event_id: str) -> dict | None:
    r = one(app.conn, f"SELECT * FROM {table} WHERE {'subject' if table == 'sr_price_check' else 'event_id'}=? "
                      f"ORDER BY rowid DESC LIMIT 1", (event_id,))
    return dict(r) if r else None


def coverage_rows(app: App) -> list[dict]:
    rows = []
    for e in all_rows(app.conn, "SELECT * FROM sr_event ORDER BY public_earliest"):
        e = dict(e)
        scr = one(app.conn, "SELECT sample_rank FROM sr_screen WHERE id=?", (e["screen_id"],))
        srcs = Counter(r["role"] for r in all_rows(app.conn, "SELECT role FROM sr_source WHERE event_id=?", (e["id"],)))
        gaps = [f"{g['kind']}: {g['description']}" for g in all_rows(app.conn, "SELECT * FROM sr_gap WHERE event_id=?", (e["id"],))]
        fin = {f["field"]: f for f in all_rows(app.conn, "SELECT field, value_num, value_text, null_reason, label FROM sr_fact "
                                                         "WHERE event_id=? AND source_kind IN ('XBRL','NONE','DERIVED')", (e["id"],))}
        have = [k for k in KEY_FINANCIALS if k in fin and fin[k]["value_num"] is not None]
        runway = fin.get("historical_cash_use_runway_months")
        pc = _latest(app, "sr_price_check", e["id"])
        el = _latest(app, "sr_eligibility", e["id"])
        oa = _latest(app, "sr_outcome_audit", e["id"])
        eld = json.loads(el["detail_json"]) if el else {}
        oad = json.loads(oa["detail_json"]) if oa else {}
        hz = (oad.get("stock") or {}).get("horizons", {})
        bm = {s: (oad.get("benchmarks") or {}).get(s, {}).get("horizons", {}).get("252") for s in ("SPY", "XBI")}
        flags = json.loads(e["flags_json"])
        cand = one(app.conn, "SELECT COUNT(*) AS n FROM sr_fact WHERE event_id=? AND author='DETERMINISTIC_KEYWORD'", (e["id"],))["n"]
        judged = one(app.conn, "SELECT COUNT(*) AS n FROM sr_judgment WHERE event_id=?", (e["id"],))["n"]
        unresolved = gaps + ([f"price: {json.loads(pc['detail_json']).get('reason')}"] if pc and pc["status"] != "OK" else []) + \
            [f"eligibility: {u}" for u in eld.get("undetermined", [])]
        rows.append({
            "sample_rank": scr["sample_rank"] if scr else None, "event_id": e["id"], "cik": e["cik"],
            "company_at_time": e["company_name_at_time"], "ticker_at_time": e["ticker_at_time"],
            "ticker_source": next((f.split(": ", 1)[1] for f in flags if f.startswith("TICKER_SOURCE")), None),
            "drug": e["drug"], "phase": e["phase"], "trial": e["trial_id"], "category": e["category"],
            "flags": ";".join(f for f in flags if not f.startswith(("TICKER_SOURCE", "MULTIPLE_SYMBOLS"))),
            "discovery": "EDGAR FTS main phrases", "inclusion": "INCLUDED",
            "event_documents": srcs.get("EVENT_FILING", 0) + srcs.get("EVENT_EXHIBIT", 0),
            "periodic_reports_by_cutoff": srcs.get("LATEST_10Q", 0) + srcs.get("LATEST_10K", 0),
            "timestamp_precision": e["public_precision"], "public_earliest": e["public_earliest"],
            "public_latest": e["public_latest"], "window": f"{e['pre_session']}→{e['measurement_session']}",
            "entry_session": e["entry_session"],
            "financials_present": f"{len(have)}/{len(KEY_FINANCIALS)}",
            "later_revised_values": sum(1 for f in fin.values() if (f["label"] or "").startswith("LATER_REVISED")),
            "runway_months": (runway["value_num"] or runway["value_text"] or runway["null_reason"]) if runway else None,
            "candidate_statements": cand, "retrospective_judgments": judged,
            "price_check": pc["status"] if pc else "NOT_RUN",
        "price_symbol": (json.loads(pc["detail_json"]).get("symbol_used") if pc else None),
        "price_symbol_source": (json.loads(pc["detail_json"]).get("symbol_source") if pc else None),
            "eligibility": el["status"] if el else "NOT_RUN", "decline": eld.get("decline"),
            "pre_event_market_cap": eld.get("pre_event_market_cap"),
            "entry_open_available": (oad.get("stock") or {}).get("entry_open"),
            "h21": hz.get("21"), "h63": hz.get("63"), "h126": hz.get("126"), "h252": hz.get("252"),
            "spy_252": bm["SPY"], "xbi_252": bm["XBI"],
            "outcome_status_evidence": oad.get("status_summary"),
            "unresolved": " | ".join(unresolved)[:600]})
    return rows


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def funnel(app: App) -> dict:
    d = latest_decisions(app)
    st = status(app)
    rows = coverage_rows(app)
    return {"searches": one(app.conn, "SELECT COUNT(*) AS n FROM sr_search")["n"],
            "search_errors": one(app.conn, "SELECT COUNT(*) AS n FROM sr_search WHERE error IS NOT NULL")["n"],
            "pool_accessions": len(pool(app)), "pool_issuers": len({r["cik"] for r in pool(app)}),
            "sensitivity": sensitivity_gap(app), "screened": st["screened"],
            "decisions": dict(Counter(x["decision"] for x in d.values())),
            "categories": dict(Counter(x["category"] for x in d.values())),
            "events": len(rows),
            "price_check": dict(Counter(r["price_check"] for r in rows)),
            "eligibility": dict(Counter(r["eligibility"] for r in rows)),
            "timestamp_precision": dict(Counter(r["timestamp_precision"] for r in rows)),
            "h252": dict(Counter(str(r["h252"]) for r in rows)),
            "outcome_status_evidence": dict(Counter(str(r["outcome_status_evidence"]) for r in rows)),
            "recalls_with_original_in_pool": _recall_check(app, d)}


def _recall_check(app: App, d: dict) -> dict:
    """NOT_AN_EVENT filings that recall an earlier announcement: is any earlier filing of the same issuer in the pool?"""
    p = pool(app)
    by_cik: dict[str, list[str]] = {}
    for r in p:
        by_cik.setdefault(r["cik"], []).append(r["file_date"])
    rec = [x for x in d.values() if any(str(f).startswith("RECALLS_EARLIER") for f in (x["details"].get("flags") or []))]
    found = 0
    for x in rec:
        cik = next((r["cik"] for r in p if r["accession"] == x["accession"]), None)
        fd = next((r["file_date"] for r in p if r["accession"] == x["accession"]), None)
        if cik and any(f < fd for f in by_cik.get(cik, [])):
            found += 1
    return {"recalls": len(rec), "an_earlier_filing_of_the_issuer_is_in_the_pool": found,
            "note": "a recall without an earlier pool filing suggests the original announcement used other wording or no 8-K"}
