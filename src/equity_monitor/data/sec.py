"""SEC EDGAR adapter: ticker map, submissions (filings index), XBRL companyfacts, filing text.

Access policy (https://www.sec.gov/os/accessing-edgar-data): declare a User-Agent with contact
details and stay under 10 requests/second. We throttle to ~6/s and cache raw responses.

Public availability time
- ``acceptanceDateTime`` from the submissions API is used when present. It is UTC (trailing ``Z``).
  Verified 2026-09-30 against live data: Apple's 2026-07-30 earnings 8-K (item 2.02) shows 20:30:28Z
  = 16:30 ET (the known release time), and a filing stamped 21:29Z kept its same-day filing date,
  which EDGAR only does for acceptance before 17:30 ET.
- Facts known only by filing date get ``public_at`` = 23:59:59 ET on the filed date
  (``FILED_DATE_END_OF_DAY``) so they are never usable before the next session.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from html.parser import HTMLParser

from ..app import App
from ..db.core import all_rows, insert, one
from ..util import NY, UTC, iso_utc, new_id, sha256_text
from .http import HttpClient, ProviderError
from .prices import record_check
from .rawstore import resolve_quality_issue, save_raw, upsert_quality_issue
from .securities import find_security, get_or_create_issuer, register_security, update_issuer_classification

PROVIDER = "SEC_EDGAR"
PARSER_VERSION = "sec-text-1"
TRACKED_FORMS = {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A", "20-F", "40-F", "6-K"}

# 8-K items -> (severity, label). CRITICAL items always alert (never suppressed by cooldowns).
EIGHT_K_ITEMS = {
    "1.01": ("MATERIAL", "Entry into a material definitive agreement"),
    "1.02": ("MATERIAL", "Termination of a material definitive agreement"),
    "1.03": ("CRITICAL", "Bankruptcy or receivership"),
    "1.05": ("MATERIAL", "Material cybersecurity incident"),
    "2.01": ("MATERIAL", "Completion of acquisition or disposition"),
    "2.02": ("MATERIAL", "Results of operations"),
    "2.03": ("MATERIAL", "Creation of a direct financial obligation"),
    "2.04": ("CRITICAL", "Triggering events accelerating an obligation"),
    "2.05": ("MATERIAL", "Exit or disposal costs"),
    "2.06": ("MATERIAL", "Material impairments"),
    "3.01": ("CRITICAL", "Delisting notice / failure to satisfy listing rule"),
    "3.03": ("MATERIAL", "Material modification of security holder rights"),
    "4.01": ("MATERIAL", "Change in certifying accountant"),
    "4.02": ("CRITICAL", "Non-reliance on previously issued financial statements"),
    "5.01": ("CRITICAL", "Change in control"),
    "5.02": ("MATERIAL", "Departure/appointment of directors or officers"),
    "5.07": ("INFO", "Submission of matters to a vote"),
    "7.01": ("INFO", "Regulation FD disclosure"),
    "8.01": ("INFO", "Other events"),
    "9.01": ("INFO", "Financial statements and exhibits"),
}


class SecConfigError(RuntimeError):
    pass


def make_client(app: App) -> HttpClient:
    ua = app.settings.sec_user_agent
    if not ua:
        raise SecConfigError(
            "SEC access requires a declared User-Agent with contact details. Set `sec_user_agent:` in "
            "config/user.yaml, e.g. 'Jane Doe jane@example.com'. See README 'Credentials and settings'.")
    return HttpClient(provider=PROVIDER, user_agent=ua, min_interval_s=0.15)


def cik10(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


def parse_acceptance(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.rstrip("Z").split(".")[0]
    return datetime.fromisoformat(v).replace(tzinfo=UTC)


def end_of_day_public(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=NY)


# ------------------------------------------------------------------ ticker map
def load_ticker_map(app: App, client: HttpClient) -> list[dict]:
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    resp = client.get(url)
    save_raw(app, PROVIDER, url, resp.content, "application/json", "json", note="ticker map")
    doc = resp.json()
    fields = doc["fields"]
    return [dict(zip(fields, row)) for row in doc["data"]]


def register_from_ticker(app: App, client: HttpClient, symbol: str) -> str:
    """Find a ticker in the SEC map and register (or link) issuer + security. Returns security id.

    A security first seen in a CSV import has a placeholder issuer without a CIK; it is linked here:
    the placeholder gets the CIK and name, or the security is re-pointed to the issuer that already
    holds that CIK. Reference-data changes are audited.
    """
    symbol = symbol.upper()
    for r in load_ticker_map(app, client):
        if str(r.get("ticker", "")).upper() != symbol:
            continue
        cik = cik10(r["cik"])
        existing = find_security(app.conn, symbol)
        if existing is None:
            iid = get_or_create_issuer(app.conn, app.now_iso(), name=r["name"], cik=cik)
            return register_security(app.conn, app.now_iso(), symbol, security_type="COMMON", issuer_id=iid,
                                     exchange=r.get("exchange"), source="sec_ticker_map")
        sec_row = one(app.conn, "SELECT * FROM security WHERE id=?", (existing,))
        holder = one(app.conn, "SELECT id FROM issuer WHERE cik=?", (cik,))
        if holder and holder["id"] != sec_row["issuer_id"]:
            app.conn.execute("UPDATE security SET issuer_id=? WHERE id=?", (holder["id"], existing))
        elif sec_row["issuer_id"] is None:
            iid = get_or_create_issuer(app.conn, app.now_iso(), name=r["name"], cik=cik)
            app.conn.execute("UPDATE security SET issuer_id=? WHERE id=?", (iid, existing))
        else:
            app.conn.execute("UPDATE issuer SET cik=COALESCE(cik, ?), name=? WHERE id=?", (cik, r["name"], sec_row["issuer_id"]))
        if sec_row["security_type"] == "UNKNOWN":
            app.conn.execute("UPDATE security SET security_type='COMMON', exchange=COALESCE(exchange, ?) WHERE id=?",
                             (r.get("exchange"), existing))
        app.audit("security.linked_to_sec", "security", existing, {"cik": cik, "name": r["name"]})
        return existing
    raise ProviderError(PROVIDER, f"ticker {symbol} not in SEC company_tickers_exchange.json")


# ------------------------------------------------------------------ submissions
@dataclass
class FilingSyncResult:
    new_document_ids: list[str]
    latest_accession: str | None
    checked: bool


def sync_filings(app: App, client: HttpClient, issuer_id: str, *, job_run_id: str | None = None,
                 submissions_json: bytes | None = None) -> FilingSyncResult:
    """Refresh the filing index for an issuer. Records a source_check either way."""
    iss = one(app.conn, "SELECT * FROM issuer WHERE id=?", (issuer_id,))
    if not iss["cik"]:
        raise ProviderError(PROVIDER, f"issuer {iss['name']} has no CIK")
    url = f"https://data.sec.gov/submissions/CIK{iss['cik']}.json"
    key = f"filings_refresh:{issuer_id}"
    try:
        raw = submissions_json if submissions_json is not None else client.get(url).content
    except ProviderError as exc:
        record_check(app, PROVIDER, issuer_id, "FILINGS", False, None, str(exc), job_run_id)
        upsert_quality_issue(app, key, scope="ISSUER", ref_id=issuer_id, code="FILINGS_REFRESH_FAILED",
                             severity="CRITICAL", detail=str(exc))
        raise
    raw_id = save_raw(app, PROVIDER, url, raw, "application/json", "json")
    doc = json.loads(raw)
    update_issuer_classification(app.conn, issuer_id, sic=doc.get("sic") or None,
                                 sic_description=doc.get("sicDescription") or None,
                                 fiscal_year_end=doc.get("fiscalYearEnd") or None)
    rec = doc.get("filings", {}).get("recent", {})
    new_ids = []
    latest = None
    n = len(rec.get("accessionNumber", []))
    for i in range(n):
        form = rec["form"][i]
        if form not in TRACKED_FORMS:
            continue
        acc = rec["accessionNumber"][i]
        latest = latest or acc
        if one(app.conn, "SELECT 1 FROM source_document WHERE provider=? AND accession_no=? AND doc_type=?",
               (PROVIDER, acc, form)):
            continue
        filed = date.fromisoformat(rec["filingDate"][i])
        acc_t = parse_acceptance(rec.get("acceptanceDateTime", [None] * n)[i])
        public_at, basis = (acc_t, "ACCEPTANCE_TIME") if acc_t else (end_of_day_public(filed), "FILED_DATE_END_OF_DAY")
        prim = rec.get("primaryDocument", [""] * n)[i]
        cik_int = int(iss["cik"])
        doc_url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-', '')}/{prim}" if prim else None)
        did = new_id("doc")
        insert(app.conn, "source_document", {
            "id": did, "provider": PROVIDER, "doc_type": form, "issuer_id": issuer_id, "accession_no": acc,
            "source_url": doc_url, "title": f"{form} {rec.get('reportDate', [''] * n)[i] or filed.isoformat()}",
            "fiscal_period_end": rec.get("reportDate", [None] * n)[i] or None, "filed_date": filed.isoformat(),
            "public_at": iso_utc(public_at), "public_at_basis": basis, "retrieved_at": app.now_iso(),
            "raw_object_id": raw_id, "content_hash": None, "parser_version": None, "trust": "PRIMARY",
            "items": rec.get("items", [""] * n)[i] or None,
            "limitations": "Index entry only until the document text is fetched.",
        })
        new_ids.append(did)
    record_check(app, PROVIDER, issuer_id, "FILINGS", True, latest, None, job_run_id)
    resolve_quality_issue(app, key)
    return FilingSyncResult(new_ids, latest, True)


# ------------------------------------------------------------------ companyfacts
def fetch_companyfacts(app: App, client: HttpClient, issuer_id: str) -> tuple[bytes, str]:
    iss = one(app.conn, "SELECT cik FROM issuer WHERE id=?", (issuer_id,))
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{iss['cik']}.json"
    resp = client.get(url)
    return resp.content, save_raw(app, PROVIDER, url, resp.content, "application/json", "json")


# ------------------------------------------------------------------ document text
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "ix:header"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "ix:header"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw_html: str) -> str:
    p = _TextExtractor()
    p.feed(raw_html)
    text = html.unescape("".join(p.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def split_passages(text: str, target_chars: int = 1500) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 > target_chars and buf:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out


def store_passages(app: App, document_id: str, text: str, section: str | None = None) -> int:
    if one(app.conn, "SELECT 1 FROM document_passage WHERE document_id=?", (document_id,)):
        return 0
    passages = split_passages(text)
    for i, ptxt in enumerate(passages):
        insert(app.conn, "document_passage", {"id": f"{document_id}#p{i}", "document_id": document_id, "ordinal": i,
                                              "section": section, "text": ptxt, "text_hash": sha256_text(ptxt)})
    return len(passages)


def fetch_document_text(app: App, client: HttpClient, document_id: str) -> int:
    d = one(app.conn, "SELECT * FROM source_document WHERE id=?", (document_id,))
    if not d["source_url"]:
        raise ProviderError(PROVIDER, "document has no primary document URL")
    resp = client.get(d["source_url"])
    save_raw(app, PROVIDER, d["source_url"], resp.content, resp.headers.get("Content-Type"), "htm")
    text = html_to_text(resp.content.decode("utf-8", errors="replace"))
    return store_passages(app, document_id, text)


def documents_for(app: App, issuer_id: str, as_of: datetime | None = None, forms: set[str] | None = None) -> list[dict]:
    rows = all_rows(app.conn, "SELECT * FROM source_document WHERE issuer_id=? ORDER BY public_at DESC", (issuer_id,))
    out = []
    for r in rows:
        if as_of is not None and (r["public_at"] is None or r["public_at"] > iso_utc(as_of)):
            continue
        if forms and r["doc_type"] not in forms:
            continue
        out.append(dict(r))
    return out


def filing_severity(form: str, items: str | None) -> tuple[str, str]:
    if form.startswith("8-K"):
        sev, labels = "INFO", []
        rank = {"INFO": 0, "MATERIAL": 1, "CRITICAL": 2}
        for it in (items or "").split(","):
            it = it.strip()
            if it in EIGHT_K_ITEMS:
                s, lab = EIGHT_K_ITEMS[it]
                labels.append(f"{it} {lab}")
                if rank[s] > rank[sev]:
                    sev = s
        return sev, "; ".join(labels) or "8-K"
    if form.startswith(("10-K", "10-Q", "20-F", "40-F")):
        return "MATERIAL", f"{form} periodic report" + (" (amendment)" if form.endswith("/A") else "")
    return "INFO", form
