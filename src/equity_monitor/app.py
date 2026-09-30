"""Application context: one object carrying DB connection, clock, config and paths."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config.models import Policy, UserSettings, load_policy, load_user_settings
from .db.core import audit, connect, insert, migrate, one
from .util import SYSTEM_CLOCK, Clock, iso_utc, new_id, to_json

DEFAULT_HOME = Path(os.environ.get("EQM_HOME", "var"))


@dataclass
class App:
    conn: sqlite3.Connection
    home: Path
    clock: Clock = field(default_factory=lambda: SYSTEM_CLOCK)
    policy: Policy = field(default_factory=Policy)
    settings: UserSettings = field(default_factory=UserSettings)
    actor: str = "user"

    @property
    def raw_dir(self) -> Path:
        return self.home / "raw"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    def now(self):
        return self.clock.now()

    def now_iso(self) -> str:
        return iso_utc(self.clock.now())

    def audit(self, action: str, entity: str, entity_id: str | None, detail=None) -> None:
        audit(self.conn, self.clock, self.actor, action, entity, entity_id, detail)

    # -------------------------------------------------------------- policy versions
    def policy_version_id(self) -> str:
        """Persist the active policy (idempotent by content hash) and return its id."""
        h = self.policy.content_hash()
        row = one(self.conn, "SELECT id FROM policy_version WHERE content_hash = ?", (h,))
        if row:
            return row["id"]
        pid = new_id("pol")
        insert(self.conn, "policy_version", {
            "id": pid, "name": f"{self.policy.name}@{self.policy.version}",
            "content_json": to_json(self.policy.model_dump(mode="json")), "content_hash": h,
            "status": self.policy.status, "created_at": self.now_iso(),
            "approved_at": None, "approver": None,
        })
        self.audit("policy.registered", "policy_version", pid, {"hash": h, "status": self.policy.status})
        return pid


def open_app(home: str | Path | None = None, *, policy_path: str | Path | None = None,
             settings_path: str | Path | None = None, clock: Clock | None = None,
             db_path: str | Path | None = None) -> App:
    home = Path(home) if home else DEFAULT_HOME
    home.mkdir(parents=True, exist_ok=True)
    if policy_path is None:
        cand = Path("config/policy.yaml")
        policy_path = cand if cand.exists() else None
    if settings_path is None:
        cand = Path("config/user.yaml")
        settings_path = cand if cand.exists() else None
    clock = clock or SYSTEM_CLOCK
    conn = connect(db_path or home / "equity_monitor.sqlite")
    migrate(conn, clock)
    return App(conn=conn, home=home, clock=clock, policy=load_policy(policy_path),
               settings=load_user_settings(settings_path))


def memory_app(clock: Clock | None = None, policy: Policy | None = None,
               settings: UserSettings | None = None, home: Path | None = None) -> App:
    """In-memory application used by tests and fixture demos."""
    clock = clock or Clock()
    conn = connect(":memory:")
    migrate(conn, clock)
    home = home or Path(os.environ.get("TMPDIR", "/tmp")) / f"eqm_{new_id('h')}"
    home.mkdir(parents=True, exist_ok=True)
    return App(conn=conn, home=home, clock=clock, policy=policy or Policy(),
               settings=settings or UserSettings(), actor="test")
