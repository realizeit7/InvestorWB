"""Immutable raw-object store and data-quality issue helpers."""

from __future__ import annotations

from pathlib import Path

from ..app import App
from ..db.core import insert, one
from ..util import new_id, sha256_bytes


def save_raw(app: App, provider: str, url: str | None, data: bytes, content_type: str | None = None,
             ext: str = "bin", note: str | None = None) -> str:
    """Store bytes content-addressed. Same content => same row; files are never overwritten."""
    h = sha256_bytes(data)
    row = one(app.conn, "SELECT id FROM raw_object WHERE sha256=?", (h,))
    if row:
        return row["id"]
    rel = Path(provider.lower()) / h[:2] / f"{h}.{ext}"
    path = app.raw_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(path)
    rid = new_id("raw")
    insert(app.conn, "raw_object", {
        "id": rid, "provider": provider, "url": url, "sha256": h, "path": str(rel), "content_type": content_type,
        "bytes": len(data), "retrieved_at": app.now_iso(), "note": note,
    })
    return rid


def read_raw(app: App, raw_id: str) -> bytes:
    row = one(app.conn, "SELECT path, sha256 FROM raw_object WHERE id=?", (raw_id,))
    data = (app.raw_dir / row["path"]).read_bytes()
    if sha256_bytes(data) != row["sha256"]:
        raise IOError(f"raw object {raw_id} failed integrity check")
    return data


def upsert_quality_issue(app: App, key: str, *, scope: str, ref_id: str | None, code: str, severity: str,
                         detail: str) -> None:
    row = one(app.conn, "SELECT id FROM data_quality_issue WHERE dedupe_key=?", (key,))
    now = app.now_iso()
    if row:
        app.conn.execute("UPDATE data_quality_issue SET last_seen=?, status='OPEN', detail=?, severity=? WHERE id=?",
                         (now, detail, severity, row["id"]))
        return
    insert(app.conn, "data_quality_issue", {
        "id": new_id("dq"), "dedupe_key": key, "scope": scope, "ref_id": ref_id, "issue_code": code,
        "severity": severity, "detail": detail, "first_seen": now, "last_seen": now, "status": "OPEN",
    })


def resolve_quality_issue(app: App, key: str) -> None:
    app.conn.execute("UPDATE data_quality_issue SET status='RESOLVED', last_seen=? WHERE dedupe_key=? AND status='OPEN'",
                     (app.now_iso(), key))


def open_quality_issues(app: App, ref_id: str | None = None) -> list[dict]:
    if ref_id is None:
        rows = app.conn.execute("SELECT * FROM data_quality_issue WHERE status='OPEN' ORDER BY severity, first_seen")
    else:
        rows = app.conn.execute("SELECT * FROM data_quality_issue WHERE status='OPEN' AND ref_id=?", (ref_id,))
    return [dict(r) for r in rows.fetchall()]
