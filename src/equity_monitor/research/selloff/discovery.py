"""Event discovery from historical filings (EDGAR full-text search), with every query, page and reported total stored.

The pool is every distinct 8-K accession returned by the protocol's MAIN phrases within the observation period. Its
screening order is fixed by the protocol (ascending sha256("sr-0.1:" + accession)), so it is reproducible and
unrelated to outcomes. SENSITIVITY phrases are searched only to estimate what the main phrases miss.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Callable

from ...app import App
from ...data.http import HttpClient, ProviderError
from ...data.rawstore import save_raw
from ...db.core import all_rows, insert, one
from ...util import new_id
from .home import load_protocol, protocol_hash

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
PAGE = 100

Search = Callable[[str, str, str, int], dict]       # (phrase, start, end, from) -> EDGAR FTS JSON


def make_search(app: App) -> Search:
    ua = app.settings.sec_user_agent
    if not ua:
        raise ProviderError("sec_fts", "set sec_user_agent (name + email) for SEC requests")
    client = HttpClient(provider="sec_fts", user_agent=ua, min_interval_s=0.25, timeout_s=60, max_retries=5)

    def search(phrase: str, start: str, end: str, frm: int) -> dict:
        resp = client.get(FTS_URL, params={"q": phrase, "forms": "8-K", "dateRange": "custom", "startdt": start,
                                           "enddt": end, "from": frm})
        d = resp.json()
        if "hits" not in d:
            raise ProviderError("sec_fts", f"unexpected response: {str(d)[:200]}")
        d["_raw"] = resp.content
        return d
    return search


def _years(start: str, end: str) -> list[tuple[str, str]]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [(max(a, date(y, 1, 1)).isoformat(), min(b, date(y, 12, 31)).isoformat()) for y in range(a.year, b.year + 1)]


def run_discovery(app: App, search: Search | None = None, *, purposes: tuple[str, ...] = ("MAIN", "SENSITIVITY")) -> dict:
    """Run every protocol query (all pages); idempotent per (phrase, period, page) for the current protocol."""
    proto, _h, _t = load_protocol()
    ph = protocol_hash(app)
    search = search or make_search(app)
    period = proto["observation_period"]
    phrases = {"MAIN": proto["discovery"]["phrases"], "SENSITIVITY": proto["discovery"]["sensitivity_phrases"]}
    out = {"queries": 0, "errors": 0, "new_hits": 0}
    for purpose in purposes:
        for phrase in phrases[purpose]:
            for start, end in _years(period["start"], period["end"]):
                frm = 0
                while True:
                    if one(app.conn, "SELECT 1 FROM sr_search WHERE protocol_hash=? AND phrase=? AND start_date=? AND "
                                     "end_date=? AND page_from=? AND error IS NULL", (ph, phrase, start, end, frm)):
                        prev = one(app.conn, "SELECT total_reported FROM sr_search WHERE protocol_hash=? AND phrase=? AND "
                                             "start_date=? AND end_date=? AND page_from=? AND error IS NULL",
                                   (ph, phrase, start, end, frm))
                        frm += PAGE
                        if frm >= (prev["total_reported"] or 0):
                            break
                        continue
                    sid = new_id("srs")
                    try:
                        d = search(phrase, start, end, frm)
                    except ProviderError as exc:
                        insert(app.conn, "sr_search", {"id": sid, "protocol_hash": ph, "purpose": purpose, "phrase": phrase,
                                                       "start_date": start, "end_date": end, "page_from": frm,
                                                       "total_reported": None, "total_relation": None, "returned": 0,
                                                       "raw_object_id": None, "error": str(exc)[:500],
                                                       "executed_at": app.now_iso()})
                        out["errors"] += 1
                        break
                    raw = d.pop("_raw", None)
                    rid = save_raw(app, "sec_fts", f"{FTS_URL}?q={phrase}&{start}..{end}&from={frm}",
                                   raw or json.dumps(d).encode(), "application/json", "json") if raw or d else None
                    hits = d["hits"]["hits"]
                    total = d["hits"]["total"]
                    insert(app.conn, "sr_search", {"id": sid, "protocol_hash": ph, "purpose": purpose, "phrase": phrase,
                                                   "start_date": start, "end_date": end, "page_from": frm,
                                                   "total_reported": total.get("value"), "total_relation": total.get("relation"),
                                                   "returned": len(hits), "raw_object_id": rid, "error": None,
                                                   "executed_at": app.now_iso()})
                    out["queries"] += 1
                    for h in hits:
                        s = h["_source"]
                        acc = s.get("adsh") or h["_id"].split(":")[0]
                        out["new_hits"] += insert(app.conn, "sr_hit", {
                            "id": new_id("srh"), "search_id": sid, "accession": acc,
                            "file_name": h["_id"].split(":", 1)[1] if ":" in h["_id"] else None,
                            "cik": (s.get("ciks") or [""])[0].lstrip("0"), "form": s.get("form"),
                            "file_date": s.get("file_date"), "display_name": "; ".join(s.get("display_names") or []),
                            "items": ",".join(s.get("items") or []), "sics": ",".join(s.get("sics") or [])},
                            or_ignore=True)
                    frm += PAGE
                    if frm >= (total.get("value") or 0) or not hits:
                        break
    app.audit("selloff.discovery", "sr_search", ph, out)
    return out


def sample_key(protocol_version: str, accession: str) -> str:
    return hashlib.sha256(f"{protocol_version}:{accession}".encode()).hexdigest()


def pool(app: App, purpose: str = "MAIN") -> list[dict]:
    """Distinct accessions from the given purpose's searches, in the protocol's fixed screening order."""
    proto, _h, _t = load_protocol()
    ph = protocol_hash(app)
    rows = all_rows(app.conn, "SELECT h.accession, h.cik, MIN(h.file_date) AS file_date, GROUP_CONCAT(DISTINCT s.phrase) AS phrases, "
                              "GROUP_CONCAT(DISTINCT h.file_name) AS files, MAX(h.display_name) AS display_name "
                              "FROM sr_hit h JOIN sr_search s ON s.id=h.search_id WHERE s.protocol_hash=? AND s.purpose=? "
                              "GROUP BY h.accession", (ph, purpose))
    out = [dict(r) | {"sample_key": sample_key(proto["protocol_version"], r["accession"])} for r in rows]
    out.sort(key=lambda r: r["sample_key"])
    for i, r in enumerate(out, 1):
        r["sample_rank"] = i
    return out


def sensitivity_gap(app: App) -> dict:
    """Issuers found only by the sensitivity phrases: a LOWER BOUND on what the main phrases miss (not screened)."""
    main = {r["cik"] for r in pool(app, "MAIN")}
    sens = pool(app, "SENSITIVITY")
    extra = sorted({r["cik"] for r in sens} - main)
    return {"main_issuers": len(main), "sensitivity_issuers": len({r["cik"] for r in sens}),
            "issuers_only_in_sensitivity": len(extra),
            "note": "issuers matched only by alternative wording; how many had a qualifying event is unknown (not screened)"}
