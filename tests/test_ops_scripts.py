"""Operational scripts report failure honestly (offline: a fake ``uv`` stands in for the app)."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_pilot(tmp_path, fail_on: str | None):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "uv"
    cond = f'case "$*" in *"{fail_on}"*) exit 3;; esac' if fail_on else ""
    fake.write_text(f"#!/usr/bin/env bash\n{cond}\nexit 0\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "EQM_SEC_USER_AGENT": "Test test@example.com"}
    return subprocess.run(["bash", str(ROOT / "scripts" / "live_pilot.sh"), str(tmp_path / "home")], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_live_pilot_exits_nonzero_when_a_command_fails(tmp_path):
    """Regression: the script printed each command's exit status but itself always returned 0."""
    p = _run_pilot(tmp_path, "sec sync MSFT")
    assert p.returncode == 1
    assert "PILOT FAILED: 1 command(s)" in p.stdout and "eqm sec sync MSFT (exit 3)" in p.stdout


def test_live_pilot_exits_nonzero_when_every_command_fails(tmp_path):
    p = _run_pilot(tmp_path, "eqm")
    assert p.returncode == 1 and "PILOT OK" not in p.stdout


def test_live_pilot_succeeds_only_when_every_command_succeeds(tmp_path):
    p = _run_pilot(tmp_path, None)
    assert p.returncode == 0 and "PILOT OK" in p.stdout
