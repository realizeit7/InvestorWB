"""Explicit, opt-in loading of KEY=VALUE environment files (``eqm --env-file PATH``).

The application never reads ``.env`` implicitly. Lines are ``KEY=VALUE`` (optional ``export`` prefix, ``#``
comments, optional single/double quotes). Variables already set in the process environment win. Values are never
printed or logged; only the key names loaded are returned.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file(path: str | Path, *, override: bool = False) -> list[str]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"env file not found: {p}")
    loaded = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            raise ValueError(f"{p}: unparseable line (expected KEY=VALUE); the value is not shown")
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if override or key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded
