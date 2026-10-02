"""Citation integrity and substantive claim support — two separate checks.

1. **Citation integrity** (``citation_status``): every citation references an existing passage (or normalized fact) of
   the SAME issuer, public at or before ``as_of``, and a passage quote appears verbatim (case/whitespace-insensitive).
   A broken citation makes the claim FAILED. An intact citation only proves the quote is real: ``SOURCE_MATCHED``.
2. **Substantive support** (``support_status``): the claim is parsed into quantitative statements (metric, value, scale,
   unit, sign, direction, period) and each is checked deterministically against the statements in the cited sentences
   (the whole source sentence(s) containing the quote, not just the quote) or the cited facts.

Final status:
  VERIFIED       every quantitative statement is confirmed on metric, value, scale, unit, sign, direction and period,
                 and the claim contains no other free-text assertion;
  FAILED         a citation is broken, or a statement is contradicted (value/scale, sign, direction, period) or its
                 number appears nowhere in the cited evidence;
  SOURCE_MATCHED citations are intact but the claim (or part of it) is free text or cannot be checked field by field —
                 review required, never treated as verified;
  UNVERIFIED     no citation; NOT_REQUIRED for ASSUMPTION/OPINION.

Only VERIFIED feeds actionable decisions. An LLM's opinion that a claim is supported is recorded as a note and never
upgrades a status (``with_llm_assessment``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from ..app import App
from ..db.core import one
from ..util import iso_utc

VERIFIER_VERSION = "ev-5"
VERIFIED, SOURCE_MATCHED, UNVERIFIED, FAILED, NOT_REQUIRED = "VERIFIED", "SOURCE_MATCHED", "UNVERIFIED", "FAILED", "NOT_REQUIRED"


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passage_id: str | None = None
    fact_id: str | None = None
    quote: str | None = Field(default=None, max_length=2000)


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=2000)
    claim_type: str = Field(pattern="^(FACT|ASSUMPTION|OPINION)$")
    citations: list[Citation] = Field(default_factory=list, max_length=10)
    supports: bool = True


@dataclass
class Verification:
    status: str                      # VERIFIED | SOURCE_MATCHED | UNVERIFIED | FAILED | NOT_REQUIRED
    details: list[str] = field(default_factory=list)
    valid_citations: list[dict] = field(default_factory=list)
    citation_status: str | None = None     # SOURCE_MATCHED | FAILED | None (no citations / not required)
    support_status: str | None = None      # CONFIRMED | CONTRADICTED | UNSUPPORTED | PARTIAL | NOT_CHECKABLE
    statements: list[dict] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED


# ----------------------------------------------------------------------------------------------- vocabulary
# phrase -> metric key (longest phrase wins); metric key -> normalized fact concepts it may be checked against
METRIC_PHRASES: dict[str, str] = {
    "net revenues": "revenue", "net revenue": "revenue", "total revenues": "revenue", "total revenue": "revenue",
    "revenues": "revenue", "revenue": "revenue", "net sales": "revenue", "sales": "revenue", "turnover": "revenue",
    "operating income": "operating_income", "income from operations": "operating_income", "operating profit": "operating_income",
    "operating loss": "operating_income-", "ebit": "operating_income",
    "operating margin": "operating_margin", "gross margin": "gross_margin", "net margin": "net_margin",
    "net income": "net_income", "net earnings": "net_income", "net profit": "net_income", "net loss": "net_income-",
    "pretax income": "pretax_income", "income before income taxes": "pretax_income",
    "operating cash flow": "cfo", "cash from operations": "cfo", "cash flow from operations": "cfo",
    "net cash provided by operating activities": "cfo",
    "capital expenditures": "capex", "capital expenditure": "capex", "capex": "capex",
    "free cash flow": "fcf", "depreciation and amortization": "dna",
    "stock-based compensation": "sbc", "share-based compensation": "sbc", "interest expense": "interest_expense",
    "share repurchases": "buybacks", "buybacks": "buybacks", "dividends paid": "dividends_paid",
    "diluted earnings per share": "eps", "diluted eps": "eps", "earnings per share": "eps", "eps": "eps",
    "diluted shares": "shares_diluted_weighted", "shares outstanding": "shares_outstanding",
    "cash and cash equivalents": "cash", "cash and equivalents": "cash", "short-term investments": "short_term_investments",
    "long-term debt (noncurrent)": "long_term_debt_noncurrent", "noncurrent long-term debt": "long_term_debt_noncurrent",
    "current maturities of long-term debt": "long_term_debt_current", "current portion of long-term debt": "long_term_debt_current",
    "long-term debt": "long_term_debt", "current debt": "debt_current", "short-term borrowings": "short_term_borrowings",
    "short-term debt": "short_term_borrowings", "commercial paper": "commercial_paper", "total debt": "total_debt",
    "operating lease liabilities": "operating_lease_liabilities", "minority interest": "minority_interest",
    "noncontrolling interest": "minority_interest", "total equity": "total_equity", "stockholders' equity": "total_equity",
    "shareholders' equity": "total_equity", "total assets": "total_assets",
}
METRIC_CONCEPTS: dict[str, set[str]] = {
    "long_term_debt": {"long_term_debt_total", "long_term_debt_noncurrent"},
    "total_debt": set(), "fcf": set(), "eps": set(), "operating_margin": set(), "gross_margin": set(), "net_margin": set(),
}
_PHRASES = sorted(METRIC_PHRASES, key=len, reverse=True)
_METRIC_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(p) for p in _PHRASES) + r")(?![\w-])")
_UP = r"increas\w*|grew|grow|grows|growth|rose|rise|rises|risen|up|higher|gain\w*|improv\w*|expand\w*|climb\w*|jump\w*"
_DOWN = r"decreas\w*|declin\w*|fell|fall|falls|fallen|down|lower|drop\w*|contract\w*|shr[ai]nk\w*|reduc\w*|slid|slump\w*"
_DIR_RE = re.compile(rf"\b(?:(?P<up>{_UP})|(?P<down>{_DOWN}))\b")
_NEG_RE = re.compile(r"\b(?:not|no|never|neither|nor|without|n't)\b|n't\b")
_QWORD = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_PERIOD_RES = [
    re.compile(r"\b(?P<qw>first|second|third|fourth) (?:fiscal )?quarter(?: of)?(?: (?:fiscal(?: year)? )?(?P<y>(?:19|20)\d{2}))?"),
    re.compile(r"\bq(?P<q>[1-4])(?:\s*(?:fy|fiscal)?\s*'?(?P<y>(?:19|20)?\d{2}))?\b"),
    re.compile(r"\b(?:fy|fiscal(?: year)?)\s*'?(?P<y>(?:19|20)?\d{2})\b"),
    re.compile(r"\b(?P<y>(?:19|20)\d{2})\b"),
]
_WS = re.compile(r"\s+")
_NUM = re.compile(r"(?<![\w.])[-(]?\$?\d[\d,]*(?:\.\d+)?%?\)?(?:\s*(?:trillion|billion|million|thousand|tn|bn|mm|m|k)\b)?", re.I)
SCALE = {"trillion": Decimal(10) ** 12, "tn": Decimal(10) ** 12, "billion": Decimal(10) ** 9, "bn": Decimal(10) ** 9,
         "million": Decimal(10) ** 6, "mm": Decimal(10) ** 6, "m": Decimal(10) ** 6, "thousand": Decimal(1000), "k": Decimal(1000)}
TOL = Decimal("0.005")       # default; the active value is policy.recommendation.claim_value_tolerance
# words that carry no checkable assertion of their own (numbers, metrics, directions and periods are checked structurally)
_FILLER = set("""a an the to of in for from by at on and or was were is are be been being its it their our company company's
year years fiscal quarter quarterly annual compared with versus vs prior previous same period reported approximately about
roughly nearly total totaled totalled filed figures figure usd dollars dollar percent percentage points point basis bps shares
share as per this that which during ended ending end amount amounted reached reaching over while respectively also
""".split())


def norm_text(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-").replace("−", "-")
    return _WS.sub(" ", s).strip().lower()


@dataclass
class Quantity:
    token: str
    value: Decimal          # signed, scaled
    unit: str | None        # % | USD | shares | bps | None
    start: int
    end: int
    metric: str | None = None
    direction: str | None = None     # UP | DOWN
    year: int | None = None
    quarter: int | None = None
    negated: bool = False
    period_inferred: bool = False    # True when the period was not stated next to the figure (document default or a
                                     # distant token): such a period can fail to confirm, but never CONTRADICT a claim
    role: str | None = None          # LEVEL (value for the stated/current period) | PRIOR (from/comparison value)
                                     # | CHANGE (change amount or rate) | None (relationship not determinable)

    def describe(self) -> str:
        per = f"{'Q%d ' % self.quarter if self.quarter else ''}{self.year or ''}".strip()
        return (f"{self.role or '?role'} {self.metric or '?metric'} {self.direction or ''} {self.value}"
                f"{' ' + self.unit if self.unit else ''}{' ' + per if per else ''}").replace("  ", " ")


def _quantities(text: str) -> list[Quantity]:
    """Numbers with unit, scale and sign. Identifiers (10-K, Q3, item 2.02a), bare years and small bare integers are
    not quantities."""
    out = []
    for m in _NUM.finditer(text):
        tok = m.group(0)
        nxt = text[m.end():m.end() + 2]
        if re.match(r"-?[A-Za-z]", nxt) and not re.match(r"(bn|mm|m|k|tn)\b", text[m.end():m.end() + 3], re.I):
            continue
        base = tok.split()[0] if " " in tok else re.sub(r"[a-zA-Z]+$", "", tok)
        core = re.sub(r"[^\d.]", "", base)
        if not core or core == ".":
            continue
        try:
            v = Decimal(core)
        except InvalidOperation:
            continue
        low = tok.lower()
        scaled = False
        for word, mult in SCALE.items():
            if re.search(rf"\b{word}\b", low):
                v *= mult
                scaled = True
                break
        after = text[m.end():m.end() + 12].lower()
        unit = "%" if "%" in tok or re.match(r"\s*(percent|per cent)\b", after) else \
            "bps" if re.match(r"\s*(basis points|bps)\b", after) else \
            "USD" if "$" in tok or re.match(r"\s*(usd|dollars)\b", after) else \
            "shares" if re.match(r"\s*shares\b", after) else None
        if unit is None and not scaled and (v < 10 or 1900 <= v <= 2100):
            continue
        if re.match(r"-", base) or (base.startswith("(") and tok.rstrip().endswith(")")):
            v = -v
        out.append(Quantity(tok.strip(), v, unit, m.start(), m.end()))
    return out


def extract_numbers(text: str) -> list[tuple[str, Decimal]]:
    return [(q.token, abs(q.value)) for q in _quantities(text)]


def _clauses(text: str) -> list[tuple[int, int]]:
    """Sentence/clause spans ('.', ';', ':' followed by space). Decimal points are not boundaries."""
    spans, start = [], 0
    for m in re.finditer(r"[.;:!?](?=\s|$)|,(?=\s(?:while|whereas|but|and|compared)\b)", text):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


# Supported (certifiable) grammar — the narrow claim format. Each number's ROLE is read from the words directly before it:
#   LEVEL : "<metric> was/were/is/of/at/to/reached/totaled X", "<metric>: X", "<metric> X" (filed-figure lists)
#   PRIOR : "from X", "compared with/to X", "versus X", "vs X", "against X"
#           (a PRIOR value followed by its own period — "from $3.75 billion in 2024" — is the LEVEL for that period)
#   CHANGE: "<direction verb> X" ("increased 12%", "fell $50 million"), "by X", "up/down X", "an increase/decline of X"
# Anything else has no determinable role and can never be VERIFIED (SOURCE_MATCHED, review required).
_ROLE_PRIOR = re.compile(r"\b(?:from|compared (?:with|to)|versus|vs\.?|against|relative to)\s*$")
_ROLE_CHANGE = re.compile(rf"\b(?:by|{_UP}|{_DOWN}|(?:{_UP}|{_DOWN}|change)\s+of)\s*$")
_ROLE_LEVEL = re.compile(r"(?:\b(?:to|was|were|is|are|of|at|reached|reaching|totaled|totalled|totaling|totalling|"
                         r"amounted to|stood at|came in at)|:)\s*$")
_PERIOD_LINK = re.compile(r"^\s*(?:in|for|during|of)?\s*$")


def _role(pre: str, metric_end_at_tail: bool) -> str | None:
    pre = re.sub(r"[\s$]+$", "", pre)
    if _ROLE_PRIOR.search(pre):
        return "PRIOR"
    if _ROLE_CHANGE.search(pre):
        return "CHANGE"
    if _ROLE_LEVEL.search(pre) or metric_end_at_tail:
        return "LEVEL"
    return None


def parse_statements(text: str, default_year: int | None = None) -> list[Quantity]:
    """Assign metric, direction, sign and period to every quantity in normalized ``text``."""
    text = norm_text(text)
    qs = _quantities(text)
    metrics = [(m.start(), m.end(), METRIC_PHRASES[m.group(1)]) for m in _METRIC_RE.finditer(text)]
    dirs = [(m.start(), "UP" if m.group("up") else "DOWN") for m in _DIR_RE.finditer(text)]
    periods = []
    taken: list[tuple[int, int]] = []
    for rx in _PERIOD_RES:
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in taken):
                continue
            y = m.groupdict().get("y")
            yr = int(y) + (2000 if len(y) == 2 else 0) if y else None
            q = int(m.group("q")) if m.groupdict().get("q") else _QWORD.get(m.groupdict().get("qw") or "")
            if yr is None and q is None:
                continue
            periods.append((m.start(), m.end(), yr, q))
            taken.append((m.start(), m.end()))
    clauses = _clauses(text)
    used_periods: set[int] = set()
    prev_end: dict[tuple[int, int], int] = {}
    for q in qs:
        a, b = next(((s, e) for s, e in clauses if s <= q.start < e), (0, len(text)))
        lo_q = max(a, prev_end.get((a, b), a))
        prev_end[(a, b)] = q.end
        tail_metric = any(lo_q <= m[0] and re.fullmatch(r"[\s$:(]*", text[m[1]:q.start]) for m in metrics)
        q.role = _role(text[lo_q:q.start], tail_metric)
        if q.role == "PRIOR":
            # a comparison value with its own explicit period is the level for that period
            nxt = next((p for p in periods if q.end <= p[0] < b and _PERIOD_LINK.match(text[q.end:p[0]])), None)
            if nxt is not None:
                q.role, q.year, q.quarter = "LEVEL", nxt[2], nxt[3]
                used_periods.add(nxt[0])
    for q in qs:
        a, b = next(((s, e) for s, e in clauses if s <= q.start < e), (0, len(text)))
        before = [m for m in metrics if a <= m[0] and m[1] <= q.start]
        mt = before[-1] if before else next((m for m in metrics if q.end <= m[0] < min(b, q.end + 40)), None)
        if mt:
            key = mt[2]
            if key.endswith("-"):
                key = key[:-1]
                if q.unit != "%" and q.value > 0:
                    q.value = -q.value         # a reported loss is a negative income figure
            q.metric = key
        lo = mt[1] if mt and mt[1] <= q.start else a
        ds = [d for d in dirs if lo <= d[0] < q.start]
        q.direction = ds[-1][1] if ds else None
        q.negated = bool(_NEG_RE.search(text[lo:q.start]))
        if q.year is not None or q.quarter is not None:
            continue                       # explicit period already attached (PRIOR -> LEVEL above)
        if q.role == "PRIOR":
            continue                       # the comparison period is not stated: unknown, never the current period
        near = [p for p in periods if a <= p[0] < b and not (p[0] <= q.start < p[1]) and p[0] not in used_periods]
        if near:
            p = min(near, key=lambda p: (min(abs(p[0] - q.end), abs(q.start - p[1])), p[0] < q.start))
            q.year, q.quarter = p[2], p[3]
            q.period_inferred = min(abs(p[0] - q.end), abs(q.start - p[1])) > 40
        elif default_year:
            q.year = default_year
            q.period_inferred = True
    return qs


def _inconsistent(stmts: list[Quantity]) -> str | None:
    """A claim whose own from/to values contradict its stated direction ("increased from 4 to 3")."""
    for lv in stmts:
        if lv.role != "LEVEL":
            continue
        for pr in stmts:
            if pr.role == "PRIOR" and pr.metric == lv.metric and pr.unit == lv.unit and pr.start < lv.start + 200:
                d = lv.direction or pr.direction
                if (d == "UP" and lv.value < pr.value) or (d == "DOWN" and lv.value > pr.value):
                    return f"claim is internally inconsistent: {d} from {pr.value} to {lv.value} for {lv.metric}"
    return None


def _close(a: Decimal, b: Decimal, tol: Decimal = TOL) -> bool:
    return a == b if b == 0 else abs(a - b) <= abs(b) * tol


def _units_compatible(a: str | None, b: str | None) -> bool:
    if a == b:
        return True
    pct = {"%", "bps"}
    return (a is None and b not in pct) or (b is None and a not in pct)


def _periods(c: Quantity, s: Quantity) -> str:
    """'ok' (compatible and confirmed), 'unknown' (claim asserts a period the source does not state), 'conflict'."""
    for cv, sv in ((c.year, s.year), (c.quarter, s.quarter)):
        if cv is not None and sv is not None and cv != sv:
            return "conflict"
    if (c.year is not None and s.year is None) or (c.quarter is not None and s.quarter is None):
        return "unknown"
    return "ok"


def _same_metric(a: str | None, b: str | None) -> bool:
    """Claim metric vs source metric/fact concept: equal, or the concept is one the metric may be checked against."""
    return a is not None and b is not None and (a == b or b in METRIC_CONCEPTS.get(a, ()) or a in METRIC_CONCEPTS.get(b, ()))


def _source_direction(c: Quantity, s: Quantity, sources: list[Quantity], tol: Decimal) -> tuple[str | None, str]:
    """Direction the evidence supports for the claim's matched quantity: the source's own direction word, else the
    comparison it states (a PRIOR value of the same metric, or a LEVEL of an earlier period, e.g. a cited prior-year
    fact). Returns (UP|DOWN|None, basis)."""
    if s.direction:
        return s.direction, f"source says {s.direction}"
    for o in sources:
        if o is s or not _same_metric(c.metric, o.metric) or o.unit != s.unit or o.negated != s.negated:
            continue
        if s.role == "LEVEL" and (o.role == "PRIOR" or (o.role == "LEVEL" and o.year and s.year and o.year < s.year
                                                         and (o.quarter or 0) == (s.quarter or 0))):
            if not _close(s.value, o.value, tol):
                return ("UP" if s.value > o.value else "DOWN"), f"source compares {o.value} -> {s.value}"
        if s.role == "PRIOR" and o.role == "LEVEL" and not _close(s.value, o.value, tol):
            return ("UP" if o.value > s.value else "DOWN"), f"source compares {s.value} -> {o.value}"
    return None, "the cited evidence states no comparison for this figure"


def _check(c: Quantity, sources: list[Quantity], tol: Decimal = TOL) -> tuple[str, str]:
    """Return (CONFIRMED|CONTRADICTED|UNSUPPORTED|UNCONFIRMED, reason) for one claim statement. A statement is
    confirmed only against a source statement with the same metric AND the same role (level / prior / change);
    a number that merely appears elsewhere under the metric is not support."""
    same_metric = [s for s in sources if _same_metric(c.metric, s.metric) and _units_compatible(c.unit, s.unit)]
    same_role = [s for s in same_metric if c.role is not None and s.role == c.role]
    for s in same_role:
        if _close(c.value, s.value, tol) and _periods(c, s) == "ok" and c.negated == s.negated:
            if c.direction is None:
                return "CONFIRMED", f"'{c.token}' matches source '{s.token}' ({s.describe()})"
            # every asserted direction must be supported, whatever the role ("decreased TO $4 billion" included)
            sd, basis = (s.direction, "source says " + str(s.direction)) if c.role == "CHANGE" else \
                _source_direction(c, s, sources, tol)
            if sd == c.direction:
                return "CONFIRMED", f"'{c.token}' matches source '{s.token}' ({s.describe()}); direction {sd}: {basis}"
            if sd is not None:
                return "CONTRADICTED", f"direction: claim says {c.direction}, evidence says {sd} for {c.metric} ({basis})"
            return "UNCONFIRMED", f"'{c.token}' matches, but its direction {c.direction} is not established: {basis}"
    for s in same_role:
        per = _periods(c, s)
        if per == "conflict" and s.period_inferred:
            continue                     # the source never stated this figure's period: cannot contradict on period
        if per == "conflict" and _close(abs(c.value), abs(s.value), tol):
            return "CONTRADICTED", f"period: claim {c.describe()} vs source {s.describe()}"
        if per == "conflict" or c.negated != s.negated:
            continue
        if _close(-c.value, s.value, tol) and c.value != 0:
            return "CONTRADICTED", f"sign: claim {c.value} vs source {s.value} for {c.metric}"
        if c.role == "CHANGE" and _close(c.value, s.value, tol) and c.direction and s.direction and c.direction != s.direction:
            return "CONTRADICTED", f"direction: claim says {c.direction}, source says {s.direction} for {c.metric}"
        if (c.role != "CHANGE" or c.direction is None or s.direction is None or c.direction == s.direction) \
                and c.unit == s.unit and not _close(c.value, s.value, tol) \
                and not any(_close(c.value, o.value, tol) and _periods(c, o) == "ok" for o in same_role):
            return "CONTRADICTED", (f"value/scale: claim {c.describe()} vs source {s.describe()}"
                                    + (" (from/to or prior/current values swapped?)" if c.role in ("LEVEL", "PRIOR") else ""))
    near = [s for s in sources if _close(abs(c.value), abs(s.value), tol) and _units_compatible(c.unit, s.unit)]
    if not near:
        return "UNSUPPORTED", f"number '{c.token}' is not supported by the cited evidence"
    if c.metric is None:
        return "UNCONFIRMED", f"'{c.token}' appears in the source but the claim names no checkable metric"
    if c.role is None:
        return "UNCONFIRMED", (f"'{c.token}': its relationship (current level, prior/comparison value or change) cannot "
                               "be determined from the wording; review required")
    s = near[0]
    why = "metric" if not _same_metric(c.metric, s.metric) else \
        ("role (level vs prior/comparison vs change)" if s.role != c.role else
         ("period (not stated next to the figure in the source)" if s.period_inferred else "period")
         if _periods(c, s) != "ok" else "direction" if c.direction != s.direction else "negation/sign")
    return "UNCONFIRMED", f"'{c.token}' appears in the source but its {why} could not be confirmed ({s.describe()})"


def _free_text(claim_text: str, quantities: list[Quantity]) -> list[str]:
    t = norm_text(claim_text)
    for q in sorted(quantities, key=lambda q: -q.start):
        t = t[:q.start] + " " + t[q.end:]
    t = _METRIC_RE.sub(" ", t)
    t = _DIR_RE.sub(" ", t)
    for rx in _PERIOD_RES:
        t = rx.sub(" ", t)
    words = re.findall(r"[a-z][a-z'-]*", t)
    return [w for w in words if w not in _FILLER and w.strip("'") not in _FILLER]


def _sentences_around(passage: str, quote: str) -> str:
    """The full source sentence(s) containing the quote, so a claim is judged on context, not a fragment."""
    p, q = norm_text(passage), norm_text(quote)
    i = p.find(q)
    spans, start = [], 0
    for m in re.finditer(r"[.!?](?=\s|$)", p):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(p):
        spans.append((start, len(p)))
    hit = [p[a:b].strip() for a, b in spans if a < i + len(q) and b > i]
    return " ".join(hit) if hit else q


def _fact_quantity(f) -> Quantity:
    concept = f["concept"]
    unit = {"USD": "USD", "shares": "shares"}.get(f["unit"], None)
    fp = f["fiscal_period"] or ""
    return Quantity(f"fact {concept}={f['value']}", Decimal(f["value"]), unit, 0, 0, metric=concept, role="LEVEL",
                    year=f["fiscal_year"] or int(f["period_end"][:4]),
                    quarter=int(fp[1]) if len(fp) == 2 and fp[0] == "Q" and fp[1].isdigit() else None)


def with_llm_assessment(v: Verification, llm_says_supported: bool | None, note: str = "") -> Verification:
    """Attach an LLM's support opinion. It is never certainty: it cannot raise a status (at most it adds a note, and
    an LLM that finds the claim unsupported keeps it out of VERIFIED)."""
    if llm_says_supported is None:
        return v
    details = v.details + [f"LLM assessment (not certainty): {'supported' if llm_says_supported else 'NOT supported'}"
                           + (f" — {note}" if note else "")]
    status = v.status
    if not llm_says_supported and status == VERIFIED:
        status = SOURCE_MATCHED        # disagreement means a human must look
    return Verification(status, details, v.valid_citations, v.citation_status, v.support_status, v.statements)


def verify_claim(app: App, claim: ClaimIn, issuer_id: str, as_of: datetime) -> Verification:
    if claim.claim_type != "FACT":
        return Verification(NOT_REQUIRED, ["labelled as " + claim.claim_type])
    if not claim.citations:
        return Verification(UNVERIFIED, ["no citation provided"])
    cutoff = iso_utc(as_of)
    details, valid, sources = [], [], []
    failed = False
    for c in claim.citations:
        if c.passage_id:
            p = one(app.conn, "SELECT p.text, d.issuer_id, d.public_at, d.id AS doc_id, d.fiscal_period_end, d.doc_type "
                              "FROM document_passage p JOIN source_document d ON d.id=p.document_id WHERE p.id=?", (c.passage_id,))
            if p is None:
                failed = True
                details.append(f"passage {c.passage_id} does not exist")
                continue
            if p["issuer_id"] != issuer_id:
                failed = True
                details.append(f"passage {c.passage_id} belongs to another issuer")
                continue
            if p["public_at"] is None or p["public_at"] > cutoff:
                failed = True
                details.append(f"passage {c.passage_id} was not public as of {cutoff}")
                continue
            if not c.quote or norm_text(c.quote) not in norm_text(p["text"]):
                failed = True
                details.append(f"quote not found verbatim in {c.passage_id}")
                continue
            periodic = (p["doc_type"] or "").split("/")[0] in ("10-K", "10-Q", "20-F", "40-F")
            default_year = int(p["fiscal_period_end"][:4]) if p["fiscal_period_end"] and periodic else None
            sources += parse_statements(_sentences_around(p["text"], c.quote), default_year)
            valid.append({"passage_id": c.passage_id, "document_id": p["doc_id"], "quote": c.quote})
        elif c.fact_id:
            f = one(app.conn, "SELECT issuer_id, public_at, value, concept, unit, period_end, fiscal_year, fiscal_period "
                              "FROM financial_fact WHERE id=?", (c.fact_id,))
            if f is None or f["issuer_id"] != issuer_id:
                failed = True
                details.append(f"fact {c.fact_id} does not exist for this issuer")
                continue
            if f["public_at"] > cutoff:
                failed = True
                details.append(f"fact {c.fact_id} was not public as of {cutoff}")
                continue
            if f["value"] is not None:
                sources.append(_fact_quantity(f))
            valid.append({"fact_id": c.fact_id})
        else:
            failed = True
            details.append("citation has neither passage_id nor fact_id")
    if failed:
        return Verification(FAILED, details, valid, citation_status=FAILED)
    stmts = parse_statements(claim.text)
    incons = _inconsistent(stmts)
    if incons:
        return Verification(FAILED, details + [incons], valid, SOURCE_MATCHED, "CONTRADICTED",
                            [{"statement": q.describe(), "result": "CONTRADICTED", "reason": incons} for q in stmts])
    tol = app.policy.recommendation.claim_value_tolerance
    results = [(q, *_check(q, sources, tol)) for q in stmts]
    recs = [{"statement": q.describe(), "result": r, "reason": why} for q, r, why in results]
    details += [f"{r}: {why}" for _q, r, why in results]
    bad = [r for _q, r, _w in results if r in ("CONTRADICTED", "UNSUPPORTED")]
    if bad:
        support = "CONTRADICTED" if "CONTRADICTED" in bad else "UNSUPPORTED"
        return Verification(FAILED, details, valid, SOURCE_MATCHED, support, recs)
    free = _free_text(claim.text, stmts)
    if not stmts:
        return Verification(SOURCE_MATCHED, details + ["free-text claim: citation is intact but the content is not "
                                                       "machine-checkable; review required"], valid, SOURCE_MATCHED, "NOT_CHECKABLE", recs)
    if any(r != "CONFIRMED" for _q, r, _w in results) or free:
        if free:
            details.append("free-text content not checked: " + " ".join(dict.fromkeys(free)))
        return Verification(SOURCE_MATCHED, details, valid, SOURCE_MATCHED, "PARTIAL", recs)
    return Verification(VERIFIED, details, valid, SOURCE_MATCHED, "CONFIRMED", recs)
