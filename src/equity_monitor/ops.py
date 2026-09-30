"""Backup and restore.

A backup is a single ``.tar.gz`` holding a consistent SQLite copy (online backup API) and the raw
store, plus a manifest with SHA-256 checksums. Restore verifies checksums and refuses to overwrite an
existing database unless ``force`` is set (the old one is moved aside, never deleted).
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from .app import App
from .util import sha256_bytes


def backup(app: App, dest_dir: str | Path) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = app.now().strftime("%Y%m%dT%H%M%SZ")
    out = dest_dir / f"eqm_backup_{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / "equity_monitor.sqlite"
        dst = sqlite3.connect(db_copy)
        app.conn.backup(dst)
        dst.close()
        manifest = {"created_at": app.now_iso(), "files": {}}
        manifest["files"]["equity_monitor.sqlite"] = sha256_bytes(db_copy.read_bytes())
        raw_files = sorted(p for p in app.raw_dir.rglob("*") if p.is_file()) if app.raw_dir.exists() else []
        for p in raw_files:
            manifest["files"][f"raw/{p.relative_to(app.raw_dir)}"] = sha256_bytes(p.read_bytes())
        mpath = Path(tmp) / "MANIFEST.json"
        mpath.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        with tarfile.open(out, "w:gz") as tar:
            tar.add(mpath, "MANIFEST.json")
            tar.add(db_copy, "equity_monitor.sqlite")
            for p in raw_files:
                tar.add(p, f"raw/{p.relative_to(app.raw_dir)}")
    return out


def restore(archive: str | Path, home: str | Path, *, force: bool = False) -> dict:
    home = Path(home)
    db = home / "equity_monitor.sqlite"
    if db.exists() and not force:
        raise FileExistsError(f"{db} exists; pass force=True (the existing DB is moved aside, not deleted)")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        manifest = json.loads((Path(tmp) / "MANIFEST.json").read_text())
        for rel, h in manifest["files"].items():
            if sha256_bytes((Path(tmp) / rel).read_bytes()) != h:
                raise IOError(f"checksum mismatch for {rel}; backup is corrupt")
        home.mkdir(parents=True, exist_ok=True)
        if db.exists():
            db.rename(db.with_suffix(f".sqlite.pre-restore-{manifest['created_at'].replace(':', '')}"))
            for side in (db.with_suffix(".sqlite-wal"), db.with_suffix(".sqlite-shm")):
                if side.exists():
                    side.unlink()
        (Path(tmp) / "equity_monitor.sqlite").rename(db)
        raw_src = Path(tmp) / "raw"
        if raw_src.exists():
            for p in raw_src.rglob("*"):
                if p.is_file():
                    target = home / "raw" / p.relative_to(raw_src)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        p.rename(target)
    conn = sqlite3.connect(db)
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    return {"restored_from": str(archive), "files": len(manifest["files"]), "integrity": ok}
