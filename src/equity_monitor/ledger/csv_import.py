"""CSV transaction import (schema documented in docs/CSV_SCHEMA.md).

Re-importing the same file is a no-op: every row has a deterministic dedupe key
(``external_id`` when present, otherwise a hash of the normalized row plus its occurrence
index among identical rows in the file).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..app import App
from ..data.securities import register_security
from ..db.core import insert
from ..util import D, new_id, parse_date, sha256_bytes, to_json
from .store import NewEvent, RecordResult, record_events

REQUIRED_COLUMNS = {"date", "type"}
KNOWN_COLUMNS = {
    "account", "date", "settle_date", "time", "timezone", "type", "symbol", "security_type", "quantity",
    "price", "fees", "amount", "currency", "cost_basis", "split_from", "split_to", "subtype", "link_id",
    "external_id", "note", "acquired_unknown",
}
TYPES = {"DEPOSIT", "WITHDRAWAL", "BUY", "SELL", "FEE", "DIVIDEND", "INTEREST", "SPLIT", "OPENING_POSITION",
         "OPENING_CASH", "DIVIDEND_REINVEST", "CORPORATE_ACTION"}
NEEDS_SECURITY = {"BUY", "SELL", "SPLIT", "OPENING_POSITION", "DIVIDEND_REINVEST", "CORPORATE_ACTION"}


@dataclass
class ImportReport:
    batch_id: str
    file_sha256: str
    rows_total: int
    inserted: int
    duplicates: int
    rejected: list[dict]
    already_imported_file: bool

    def summary(self) -> str:
        s = (f"rows={self.rows_total} inserted={self.inserted} duplicates={self.duplicates} "
             f"rejected={len(self.rejected)}")
        if self.already_imported_file:
            s += " (identical file imported before)"
        return s


def parse_rows(app: App, text: str) -> tuple[list[NewEvent], list[dict]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("empty CSV")
    cols = {c.strip().lower() for c in reader.fieldnames}
    missing = REQUIRED_COLUMNS - cols
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    unknown = cols - KNOWN_COLUMNS
    if unknown:
        raise ValueError(f"unknown columns (typo?): {sorted(unknown)}")
    events: list[NewEvent] = []
    errors: list[dict] = []
    occurrences: dict[str, int] = {}
    for n, raw in enumerate(reader, start=2):  # header is line 1
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            evs = _row_to_events(app, row, n)
        except ValueError as exc:
            errors.append({"row": n, "error": str(exc)})
            continue
        sig = to_json({k: v for k, v in sorted(row.items()) if k not in ("note",)})
        occ = occurrences.get(sig, 0)
        occurrences[sig] = occ + 1
        for i, e in enumerate(evs):
            e.dedupe_extra = f"{occ}:{i}"
        events.extend(evs)
    return events, errors


def _row_to_events(app: App, row: dict, n: int) -> list[NewEvent]:
    t = row.get("type", "").upper()
    if t not in TYPES:
        raise ValueError(f"unknown type {t!r}")
    d = parse_date(row.get("date"))
    if d is None:
        raise ValueError("date is required (YYYY-MM-DD)")
    tzname = row.get("timezone") or "America/New_York"
    occurred = None
    if row.get("time"):
        hh, mm = row["time"].split(":")[:2]
        occurred = datetime.combine(d, time(int(hh), int(mm)), tzinfo=ZoneInfo(tzname))
    sec_id = None
    if row.get("symbol"):
        sec_id = register_security(app.conn, app.now_iso(), row["symbol"],
                                   security_type=(row.get("security_type") or None) and row["security_type"].upper(),
                                   source="csv")
    elif t in NEEDS_SECURITY:
        raise ValueError(f"{t} requires symbol")
    fees = D(row.get("fees"))
    common = dict(trade_date=d, settle_date=parse_date(row.get("settle_date")), security_id=sec_id,
                  currency=(row.get("currency") or "USD").upper(), external_id=row.get("external_id") or None,
                  occurred_at=occurred, source_timezone=tzname if row.get("time") else None,
                  note=row.get("note") or None, import_row=n, link_group_id=row.get("link_id") or None)
    if t == "DIVIDEND_REINVEST":
        amount = D(row.get("amount"))
        qty, price = D(row.get("quantity")), D(row.get("price"))
        if amount is None or qty is None or price is None:
            raise ValueError("DIVIDEND_REINVEST requires amount, quantity and price")
        link = row.get("link_id") or f"drip:{new_id('l')}"
        ext = row.get("external_id")
        div = NewEvent(event_type="DIVIDEND", amount=amount, **{**common, "link_group_id": link,
                                                                 "external_id": f"{ext}:div" if ext else None})
        buy = NewEvent(event_type="BUY", quantity=qty, price=price, fees=fees,
                       **{**common, "link_group_id": link, "external_id": f"{ext}:buy" if ext else None})
        return [div, buy]
    if t == "SPLIT":
        num, den = D(row.get("split_to")), D(row.get("split_from"))
        if num is None or den is None:
            raise ValueError("SPLIT requires split_from and split_to (e.g. 1 and 4 for a 4-for-1 split)")
        return [NewEvent(event_type="SPLIT", ratio_num=num, ratio_den=den, **common)]
    if t == "OPENING_POSITION":
        qty = D(row.get("quantity"))
        if qty is None:
            raise ValueError("OPENING_POSITION requires quantity")
        basis = D(row.get("cost_basis"))  # blank => unknown (NOT zero)
        subtype = "UNKNOWN_ACQUIRED" if row.get("acquired_unknown", "").lower() in ("1", "true", "yes") else None
        return [NewEvent(event_type="OPENING_POSITION", quantity=qty, cost_basis_total=basis,
                         action_subtype=subtype, **common)]
    if t == "CORPORATE_ACTION":
        return [NewEvent(event_type="CORPORATE_ACTION", action_subtype=(row.get("subtype") or "OTHER").upper(),
                         quantity=D(row.get("quantity")), amount=D(row.get("amount")), **common)]
    return [NewEvent(event_type=t, quantity=D(row.get("quantity")), price=D(row.get("price")), fees=fees,
                     amount=D(row.get("amount")), **common)]


def import_csv(app: App, account_id: str, path: str | Path | None = None, *, text: str | None = None) -> ImportReport:
    if text is None:
        data = Path(path).read_bytes()  # type: ignore[arg-type]
        text = data.decode("utf-8-sig")
    else:
        data = text.encode("utf-8")
    fhash = sha256_bytes(data)
    prior = app.conn.execute("SELECT 1 FROM import_batch WHERE account_id=? AND file_sha256=? AND kind='transactions'",
                             (account_id, fhash)).fetchone()
    events, parse_errors = parse_rows(app, text)
    batch_id = new_id("imp")
    insert(app.conn, "import_batch", {
        "id": batch_id, "account_id": account_id, "kind": "transactions",
        "source_path": str(path) if path else None, "file_sha256": fhash, "imported_at": app.now_iso(),
        "rows_total": 0, "rows_inserted": 0, "rows_duplicate": 0, "rows_rejected": 0, "detail_json": None,
    })
    result: RecordResult = record_events(app, account_id, events, batch_id=batch_id)
    rejected = parse_errors + result.rejected
    rows_total = len({e.import_row for e in events}) + len(parse_errors)
    # import_batch is metadata about the import run; it is written once, then its counters are filled in.
    app.conn.execute("UPDATE import_batch SET rows_total=?, rows_inserted=?, rows_duplicate=?, rows_rejected=?,"
                     " detail_json=? WHERE id=?",
                     (rows_total, len(result.inserted), len(result.duplicates), len(rejected), to_json(rejected),
                      batch_id))
    app.audit("ledger.import", "import_batch", batch_id, {"inserted": len(result.inserted),
                                                         "duplicates": len(result.duplicates),
                                                         "rejected": len(rejected)})
    return ImportReport(batch_id, fhash, rows_total, len(result.inserted), len(result.duplicates), rejected,
                        bool(prior))
