"""SQLite connection, migrations, and small query helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..util import iso_utc, new_id, to_json, SYSTEM_CLOCK, Clock

MIGRATIONS_PACKAGE = "equity_monitor.db.migrations"


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, detect_types=0, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL" if path != ":memory:" else "PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _migration_files() -> list[tuple[str, str]]:
    files = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        if entry.name.endswith(".sql"):
            files.append((entry.name, entry.read_text(encoding="utf-8")))
    return sorted(files)


def migrate(conn: sqlite3.Connection, clock: Clock = SYSTEM_CLOCK) -> list[str]:
    """Apply pending migrations in order. Returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
    applied = []
    for name, sql in _migration_files():
        if name in done:
            continue
        # Table rebuilds (SQLite cannot alter a CHECK) must run with foreign keys off, or dropping the old table would
        # cascade/raise; integrity is re-checked with foreign_key_check before committing.
        rebuild = "-- migrate: foreign_keys=off" in sql
        if rebuild:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            if rebuild and conn.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError(f"migration {name} left foreign-key violations")
            conn.execute(
                "INSERT INTO schema_migration(name, applied_at) VALUES (?, ?)",
                (name, iso_utc(clock.now())),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            if rebuild:
                conn.execute("PRAGMA foreign_keys = ON")
        applied.append(name)
    return applied


def _split_sql(sql: str) -> Iterator[str]:
    """Split a migration into statements; triggers contain inner ';' so track BEGIN/END."""
    buf: list[str] = []
    depth = 0
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        # strip trailing comments
        if " --" in line:
            line = line.split(" --", 1)[0]
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            depth = 1
        if depth and "END;" in upper:
            depth = 0
            yield "\n".join(buf)
            buf = []
            continue
        if not depth and stripped.endswith(";"):
            yield "\n".join(buf)
            buf = []
    if buf and "".join(buf).strip():
        yield "\n".join(buf)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Nested-safe transaction using savepoints."""
    name = f"sp_{new_id('t')}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def insert(conn: sqlite3.Connection, table: str, row: dict[str, Any], or_ignore: bool = False) -> int:
    cols = list(row.keys())
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = conn.execute(sql, [row[c] for c in cols])
    return cur.rowcount


def audit(conn: sqlite3.Connection, clock: Clock, actor: str, action: str, entity: str,
          entity_id: str | None, detail: Any = None) -> None:
    insert(conn, "audit_log", {
        "id": new_id("aud"), "at": iso_utc(clock.now()), "actor": actor, "action": action,
        "entity": entity, "entity_id": entity_id,
        "detail_json": to_json(detail) if detail is not None else None,
    })
