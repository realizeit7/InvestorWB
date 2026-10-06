"""Isolated research data home: its own SQLite database (the shared schema for SEC documents, facts and prices, plus
the research-only ``sr_*`` tables), marked as a research home. Research commands refuse any other database, and the
research home refuses to be the portfolio home or a database that holds portfolio data."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from ...app import DEFAULT_HOME, App, open_app
from ...db.core import insert, one
from ...util import Clock, new_id, sha256_text

SCHEMA = Path(__file__).with_name("schema.sql")
PROTOCOL_PATH = Path("config/selloff_protocol.yaml")
DEFAULT_RESEARCH_HOME = Path(os.environ.get("EQM_SELLOFF_HOME", "var/research/selloff"))
SR_TABLES = ("research_home", "sr_protocol", "sr_search", "sr_hit", "sr_filing", "sr_screen", "sr_event", "sr_source",
             "sr_gap", "sr_fact", "sr_price_check", "sr_eligibility", "sr_outcome_audit", "sr_judgment", "sr_effort")
PORTFOLIO_TABLES = ("portfolio", "account", "ledger_event", "recommendation", "allocation_proposal", "thesis_version")


class NotAResearchHome(RuntimeError):
    pass


def _same(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def _has_portfolio_data(app: App) -> list[str]:
    found = []
    for t in PORTFOLIO_TABLES:
        if one(app.conn, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)) and \
                one(app.conn, f"SELECT 1 FROM {t} LIMIT 1"):
            found.append(t)
    return found


def _apply_schema(app: App) -> None:
    app.conn.executescript(SCHEMA.read_text())
    for t in SR_TABLES:
        for op in ("UPDATE", "DELETE"):
            app.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                             f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")


def load_protocol(path: Path | None = None) -> tuple[dict, str, str]:
    text = Path(path or PROTOCOL_PATH).read_text()
    return yaml.safe_load(text), sha256_text(text), text


def record_protocol(app: App, path: Path | None = None) -> str:
    proto, h, text = load_protocol(path)
    if not one(app.conn, "SELECT 1 FROM sr_protocol WHERE content_hash=?", (h,)):
        insert(app.conn, "sr_protocol", {"id": new_id("srp"), "version": proto["protocol_version"], "content_hash": h,
                                         "content_yaml": text, "recorded_at": app.now_iso()})
    return h


def init_home(path: str | Path | None = None, *, clock: Clock | None = None, portfolio_home: Path | None = None,
              settings_path: str | Path | None = None) -> App:
    """Create (or reopen) the research home. Refuses the portfolio home and any database holding portfolio data."""
    path = Path(path or DEFAULT_RESEARCH_HOME)
    portfolio_home = Path(portfolio_home or DEFAULT_HOME)
    if _same(path, portfolio_home):
        raise NotAResearchHome(f"{path} is the portfolio data home; use a separate research home")
    db = path / "equity_monitor.sqlite"
    if db.exists():
        app = open_app(path, clock=clock, settings_path=settings_path)
        if not one(app.conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='research_home'"):
            found = _has_portfolio_data(app)
            if found:
                app.conn.close()
                raise NotAResearchHome(f"{db} holds portfolio data ({', '.join(found)}); refusing to use it for research")
    else:
        app = open_app(path, clock=clock, settings_path=settings_path)
    _apply_schema(app)
    if not one(app.conn, "SELECT 1 FROM research_home"):
        insert(app.conn, "research_home", {"kind": "SELLOFF_RESEARCH", "created_at": app.now_iso(),
                                           "note": "research-only data home; no portfolio data, no recommendations"})
    record_protocol(app)
    return app


def open_research(path: str | Path | None = None, *, clock: Clock | None = None,
                  settings_path: str | Path | None = None) -> App:
    path = Path(path or DEFAULT_RESEARCH_HOME)
    db = path / "equity_monitor.sqlite"
    if not db.exists():
        raise NotAResearchHome(f"no research home at {path}: run `eqm research selloff init` first")
    app = open_app(path, clock=clock, settings_path=settings_path)
    if not one(app.conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='research_home'") or \
            not one(app.conn, "SELECT 1 FROM research_home WHERE kind='SELLOFF_RESEARCH'"):
        app.conn.close()
        raise NotAResearchHome(f"{db} is not a selloff research home (refusing to write research data into it)")
    _apply_schema(app)                    # idempotent; picks up new research tables
    return app


def protocol_hash(app: App) -> str:
    return record_protocol(app)
