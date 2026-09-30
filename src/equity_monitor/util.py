"""Small shared helpers: clock, decimals, identifiers, hashing, JSON.

Conventions (see docs/DESIGN.md):
- Every stored timestamp is UTC ISO-8601 with a trailing ``Z``.
- Money and quantities are ``decimal.Decimal`` and stored as TEXT; never floats.
- ``None`` means *unknown*; it is never silently replaced by zero.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, getcontext
from typing import Any, Callable
from zoneinfo import ZoneInfo

getcontext().prec = 34  # IEEE decimal128-like precision for money arithmetic

UTC = timezone.utc
NY = ZoneInfo("America/New_York")

CENT = Decimal("0.01")
SHARE_QUANTUM = Decimal("0.000001")  # brokers commonly report fractional shares to 6 dp


# --------------------------------------------------------------------------- clock
class Clock:
    """Injectable clock so tests and replays are deterministic."""

    def __init__(self, fixed: datetime | None = None):
        self._fixed = fixed

    def now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed
        return datetime.now(UTC)

    def set(self, when: datetime) -> None:
        self._fixed = ensure_utc(when)


SYSTEM_CLOCK = Clock()


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(UTC)


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(s: str | None) -> datetime | None:
    if s is None or s == "":
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return ensure_utc(dt)


def parse_date(s: str | date | None) -> date | None:
    if s is None or s == "":
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    return date.fromisoformat(str(s)[:10])


def ny_datetime(d: date, hh: int, mm: int = 0) -> datetime:
    """A wall-clock time in New York on date ``d`` (DST-aware), returned in UTC."""
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).astimezone(UTC)


# --------------------------------------------------------------------------- decimals
def D(value: Any) -> Decimal | None:
    """Parse to Decimal. Blank/None -> None (unknown), never 0."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # floats only arrive from provider JSON; go through repr to avoid binary noise
        return Decimal(repr(value))
    s = str(value).strip().replace(",", "").replace("$", "")
    if s == "" or s.lower() in {"na", "n/a", "none", "null", "nan", "unknown"}:
        return None
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def dstr(value: Decimal | None) -> str | None:
    """Canonical TEXT form for storage (None stays None)."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = D(value)
    v = value.normalize()
    # avoid scientific notation like 1E+3 in the database
    return format(v, "f")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def fmt_money(value: Decimal | str | None) -> str:
    if value is None or value == "":
        return "unknown"
    v = money(D(value))
    return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"


def fmt_pct(value: Decimal | float | str | None, digits: int = 1) -> str:
    if value is None or value == "":
        return "n/a"
    return f"{float(value) * 100:.{digits}f}%"


# --------------------------------------------------------------------------- ids & hashes
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return dstr(o)
    if isinstance(o, datetime):
        return iso_utc(o)
    if isinstance(o, date):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


def to_json(obj: Any) -> str:
    """Deterministic JSON (sorted keys) so content hashes are stable."""
    return json.dumps(obj, default=_json_default, sort_keys=True, separators=(",", ":"))


def from_json(s: str | None) -> Any:
    if s is None:
        return None
    return json.loads(s)


def stable_hash(obj: Any) -> str:
    return sha256_text(to_json(obj))


def first(items, pred: Callable[[Any], bool]):
    for it in items:
        if pred(it):
            return it
    return None
