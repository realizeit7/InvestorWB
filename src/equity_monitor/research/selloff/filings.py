"""Point-in-time filing retrieval for screened accessions and their issuers.

For each accession: the issuer's EDGAR submissions index (all pages) gives the EXACT acceptance time (document
submitted) and the issuer's other filings; the main 8-K document and the matched exhibit are stored as
``source_document`` rows with raw hash, parser version and passages. The ticker and exchange VALID AT THE TIME come
from the 8-K cover page, never from today's ticker map. A release dateline gives a date-only publication date.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Callable

from ...app import App
from ...data.http import HttpClient, ProviderError
from ...data.rawstore import save_raw
from ...data.sec import PARSER_VERSION, PROVIDER, html_to_text, parse_acceptance, store_passages
from ...data.securities import get_or_create_issuer, update_issuer_classification
from ...db.core import all_rows, insert, one
from ...util import iso_utc, new_id, sha256_bytes

ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{file}"
SUBMISSIONS = "https://data.sec.gov/submissions/{name}"

Fetch = Callable[[str], bytes]                 # url -> bytes (injectable for offline tests)

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_MON_ABBR = "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
_MONTH_RX = MONTHS + "|" + _MON_ABBR


def _month_no(name: str) -> int:
    n = name.lower().rstrip(".")[:3]
    return ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(n) + 1
_DATELINE = re.compile(rf"\b({_MONTH_RX})\.? (\d{{1,2}}), (\d{{4}})\b")
_WIRE = re.compile(r"PRNewswire|GLOBE NEWSWIRE|GlobeNewswire|Business Wire|BUSINESS WIRE|ACCESSWIRE|Accesswire|"
                   r"/PRNewswire|\s--|—|–|\)\s*-\s|\d{4}\s+-\s", re.I)
_REPORT_DATE = re.compile(rf"Date of (?:R|r)eport[^:]{{0,80}}:?\s*({_MONTH_RX})\.? (\d{{1,2}}),? (\d{{4}})", re.I)
_EXCHANGES = (("Nasdaq", re.compile(r"nasdaq", re.I)), ("NYSE American", re.compile(r"nyse american|nyse mkt", re.I)),
              ("NYSE", re.compile(r"new york stock exchange|\bnyse\b", re.I)))


def make_fetch(app: App) -> Fetch:
    ua = app.settings.sec_user_agent
    if not ua:
        raise ProviderError(PROVIDER, "set sec_user_agent (name + email) for SEC requests")
    client = HttpClient(provider=PROVIDER, user_agent=ua, min_interval_s=0.15, timeout_s=60, max_retries=4)
    return lambda url: client.get(url).content


# ------------------------------------------------------------------ issuer filing index (all pages)
def submissions(app: App, cik: str, fetch: Fetch) -> dict:
    """Combined filing list for a CIK (recent + older pages). Cached as raw objects; re-fetched per research run."""
    cik10 = str(int(cik)).zfill(10)
    url = SUBMISSIONS.format(name=f"CIK{cik10}.json")
    raw = fetch(url)
    save_raw(app, PROVIDER, url, raw, "application/json", "json", note="selloff submissions index")
    doc = json.loads(raw)
    cols = ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "primaryDocument", "items", "reportDate")
    rows: list[dict] = []

    def add(block: dict) -> None:
        n = len(block.get("accessionNumber", []))
        for i in range(n):
            rows.append({c: (block.get(c) or [None] * n)[i] for c in cols})
    add(doc.get("filings", {}).get("recent", {}))
    for f in doc.get("filings", {}).get("files", []):
        u = SUBMISSIONS.format(name=f["name"])
        r2 = fetch(u)
        save_raw(app, PROVIDER, u, r2, "application/json", "json")
        add(json.loads(r2))
    return {"name": doc.get("name"), "sic": doc.get("sic"), "sicDescription": doc.get("sicDescription"),
            "fiscalYearEnd": doc.get("fiscalYearEnd"), "former_names": doc.get("formerNames") or [],
            "former_names_list": [n.get("name") for n in doc.get("formerNames") or [] if n.get("name")],
            "current_tickers": doc.get("tickers") or [], "filings": rows}


def ensure_issuer(app: App, cik: str, sub: dict) -> str:
    iid = get_or_create_issuer(app.conn, app.now_iso(), name=sub.get("name") or f"CIK {cik}", cik=str(cik))
    update_issuer_classification(app.conn, iid, sic=sub.get("sic") or None, sic_description=sub.get("sicDescription"),
                                 fiscal_year_end=sub.get("fiscalYearEnd"))
    return iid


def store_document(app: App, fetch: Fetch, *, cik: str, issuer_id: str, accession: str, file_name: str, doc_type: str,
                   accepted: datetime | None, filing_date: str | None, report_date: str | None = None,
                   items: str | None = None) -> str:
    """Idempotent: one source_document per (accession, file); public_at = EDGAR acceptance (exact) when known."""
    existing = one(app.conn, "SELECT id FROM source_document WHERE provider=? AND accession_no=? AND source_url LIKE ?",
                   (PROVIDER, accession, f"%/{file_name}"))
    if existing:
        return existing["id"]
    url = ARCHIVE.format(cik=int(cik), acc=accession.replace("-", ""), file=file_name)
    raw = fetch(url)
    rid = save_raw(app, PROVIDER, url, raw, "text/html", file_name.rsplit(".", 1)[-1][:5] or "htm")
    if accepted is not None:
        pub, basis = iso_utc(accepted), "ACCEPTANCE_TIME"
    else:
        from ...data.sec import end_of_day_public
        pub, basis = iso_utc(end_of_day_public(date.fromisoformat(filing_date))), "FILED_DATE_END_OF_DAY"
    did = new_id("doc")
    insert(app.conn, "source_document", {
        "id": did, "provider": PROVIDER, "doc_type": doc_type, "issuer_id": issuer_id, "accession_no": accession,
        "source_url": url, "title": f"{doc_type} {file_name}", "fiscal_period_end": report_date or None,
        "filed_date": filing_date, "public_at": pub, "public_at_basis": basis, "retrieved_at": app.now_iso(),
        "raw_object_id": rid, "content_hash": sha256_bytes(raw), "parser_version": PARSER_VERSION, "trust": "PRIMARY",
        "items": items, "limitations": None})
    text = html_to_text(raw.decode("utf-8", errors="replace")) if file_name.lower().endswith((".htm", ".html")) else \
        raw.decode("utf-8", errors="replace")
    store_passages(app, did, text)
    return did


def doc_text(app: App, document_id: str) -> str:
    return "\n\n".join(r["text"] for r in all_rows(app.conn, "SELECT text FROM document_passage WHERE document_id=? "
                                                             "ORDER BY ordinal", (document_id,)))


# ------------------------------------------------------------------ cover page (ticker/exchange at the time)
def parse_cover(text: str) -> dict:
    """Trading symbols and exchanges as stated on an 8-K/10-Q/10-K cover page (2019+ format). Empty if absent."""
    out: dict = {"symbols": [], "exchanges": [], "company": None, "snippet": None}
    m = re.search(r"\(Exact name of registrant", text, re.I)
    if m:
        before = [ln.strip() for ln in text[max(0, m.start() - 300):m.start()].split("\n") if ln.strip()]
        out["company"] = before[-1] if before else None
    t = re.search(r"Trading\s+Symbol", text, re.I)
    if not t:
        return out
    seg = text[t.start(): t.start() + 900]
    end = re.search(r"Indicate by check mark|Emerging growth|Item\s+\d", seg, re.I)
    seg = seg[: end.start()] if end else seg
    out["snippet"] = re.sub(r"\s+", " ", seg)[:400]
    lines = [ln.strip() for ln in re.split(r"[\n|]", seg) if ln.strip()]
    for ln in lines[1:]:
        if re.fullmatch(r"[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?", ln) and ln not in ("N/A", "NONE", "LLC"):
            out["symbols"].append(ln)
        for name, rx in _EXCHANGES:
            if rx.search(ln):
                out["exchanges"].append(name)
                break
    if not out["symbols"]:      # cells run together: "Common StockMRTXThe Nasdaq Global Select Market"
        for m2 in re.finditer(r"(?<![A-Z])([A-Z]{1,5})(?=\s*(?:The\s+)?(?:Nasdaq|NASDAQ|New York Stock Exchange|NYSE))", seg):
            if m2.group(1) not in ("LLC", "THE"):
                out["symbols"].append(m2.group(1))
    if not out["exchanges"]:
        for name, rx in _EXCHANGES:
            if rx.search(seg):
                out["exchanges"].append(name)
                break
    return out


_REPORT_DATE_BEFORE = re.compile(rf"({_MONTH_RX})\.? (\d{{1,2}}),? (\d{{4}})\s*\(?Date of (?:R|r)eport", re.I)


def parse_report_date(text: str) -> str | None:
    """8-K cover: 'Date of Report (Date of earliest event reported)' — when the reported event occurred (date only)."""
    m = _REPORT_DATE.search(text[:3000]) or _REPORT_DATE_BEFORE.search(text[:3000])
    if not m:
        return None
    return date(int(m.group(3)), _month_no(m.group(1)), int(m.group(2))).isoformat()


_RELEASE_TICKER = re.compile(r"\((?:NASDAQ|Nasdaq|NYSE American|NYSE|NYSE MKT)(?:\s*(?:GS|GM|CM|Global Select|Global Market))?\s*:\s*([A-Z]{1,5})\)")


def release_ticker(text: str) -> tuple[str | None, str | None]:
    """Ticker as printed in a press release, e.g. '(NASDAQ: MRTX)' — stated in the document at the time."""
    m = _RELEASE_TICKER.search(text[:4000])
    if not m:
        return None, None
    ex = "NYSE American" if "American" in m.group(0) or "MKT" in m.group(0) else \
        "NYSE" if "NYSE" in m.group(0) else "Nasdaq"
    return m.group(1), ex


def parse_dateline(text: str) -> tuple[str | None, str | None]:
    """Date-only release dateline from the first lines of a press release (e.g. 'BOSTON, March 5, 2019 /PRNewswire/')."""
    head = text[:1200]
    for m in _DATELINE.finditer(head):
        window = head[m.start(): m.end() + 60]
        if _WIRE.search(window) or re.search(r"\(\s*(?:GLOBE|BUSINESS)", window):
            return date(int(m.group(3)), _month_no(m.group(1)), int(m.group(2))).isoformat(), re.sub(r"\s+", " ", window)[:120]
    return None, None


INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accd}-index.html"
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def exhibit_files(app: App, cik: str, accession: str, fetch: Fetch) -> list[tuple[str, str]]:
    """(file, type) for every EX-99* text exhibit listed on the filing's EDGAR index page (press releases etc.)."""
    url = INDEX.format(cik=int(cik), acc=accession.replace("-", ""), accd=accession)
    raw = fetch(url)
    save_raw(app, PROVIDER, url, raw, "text/html", "html", note="filing index")
    out = []
    for row in _ROW.findall(raw.decode("utf-8", errors="replace")):
        cells = [re.sub(r"<[^>]+>", " ", c).replace("&nbsp;", " ").strip() for c in _CELL.findall(row)]
        if len(cells) >= 4:
            name, typ = cells[2].split()[0] if cells[2].split() else "", cells[3]
            if typ.upper().startswith("EX-99") and name.lower().endswith((".htm", ".html", ".txt")):
                out.append((name, typ))
    return out


# ------------------------------------------------------------------ one screened accession
def fetch_filing(app: App, accession: str, cik: str, hit_files: list[str], fetch: Fetch,
                 sub: dict | None = None) -> dict:
    """Store the 8-K main document and matched exhibit(s); record acceptance, cover page and dateline. Idempotent."""
    prev = one(app.conn, "SELECT * FROM sr_filing WHERE accession=?", (accession,))
    if prev:
        return dict(prev)
    sub = sub or submissions(app, cik, fetch)
    iid = ensure_issuer(app, cik, sub)
    row = next((f for f in sub["filings"] if f["accessionNumber"] == accession), None)
    accepted = parse_acceptance(row["acceptanceDateTime"]) if row and row.get("acceptanceDateTime") else None
    form = row["form"] if row else "8-K"
    filing_date = row["filingDate"] if row else None
    main_id = None
    if row and row.get("primaryDocument"):
        main_id = store_document(app, fetch, cik=cik, issuer_id=iid, accession=accession, file_name=row["primaryDocument"],
                                 doc_type=form, accepted=accepted, filing_date=filing_date, items=row.get("items"))
    try:
        exhibits = [fn for fn, _t in exhibit_files(app, cik, accession, fetch)]
    except ProviderError:
        exhibits = []
    hit_id = None
    for fn in list(dict.fromkeys(list(hit_files) + exhibits)):
        if row and fn == row.get("primaryDocument"):
            hit_id = hit_id or main_id
            continue
        did = store_document(app, fetch, cik=cik, issuer_id=iid, accession=accession, file_name=fn,
                             doc_type=f"{form} EXHIBIT {fn}", accepted=accepted, filing_date=filing_date)
        hit_id = hit_id or did
    main_text = doc_text(app, main_id) if main_id else ""
    cover = parse_cover(main_text) if main_id else {"symbols": [], "exchanges": []}
    cover["report_date"] = parse_report_date(main_text) if main_id else None
    dl, dq = (None, None)
    for d in (hit_id, main_id):
        if d:
            dl, dq = parse_dateline(doc_text(app, d))
            if dl:
                break
    rec = {"accession": accession, "cik": str(int(cik)), "issuer_id": iid, "form": form,
           "accepted_at": iso_utc(accepted) if accepted else None,
           "accepted_basis": "EDGAR_ACCEPTANCE_TIME" if accepted else "UNKNOWN (accession not in submissions index)",
           "filing_date": filing_date, "main_document_id": main_id, "hit_document_id": hit_id or main_id,
           "cover_json": json.dumps(cover), "dateline_date": dl, "dateline_quote": dq, "retrieved_at": app.now_iso()}
    insert(app.conn, "sr_filing", rec)
    return rec
