"""Financial fact normalization and point-in-time access.

Storage: every as-filed XBRL value becomes one ``financial_fact`` row keyed by
(issuer, concept, period, accession). Restatements are *new rows* with a later ``public_at``;
nothing is overwritten, so an earlier as-of query can never see a later restatement.

Query (``FactView``): given an ``as_of`` timestamp,
1. keep facts with ``public_at <= as_of``;
2. per period choose the highest-priority source tag, then the latest-known filing;
3. derive discrete quarters from year-to-date durations (Q2 = 6M - Q1, Q3 = 9M - 6M,
   Q4 = FY - 9M) when a 3-month value is not reported. Derived values carry the latest
   component availability and a derivation note;
4. TTM = sum of the last four contiguous quarters, else ``None``.

Missing facts are ``None`` (with a reason where known), never zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..app import App
from ..data.sec import end_of_day_public
from ..db.core import all_rows, insert, one
from ..util import D, dstr, iso_utc, new_id, parse_utc

NORMALIZER_VERSION = "norm-2"

# canonical concept -> (unit kind, [(taxonomy, tag) in priority order], period type)
CONCEPTS: dict[str, tuple[str, list[tuple[str, str]], str]] = {
    "revenue": ("USD", [("us-gaap", "Revenues"),
                        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
                        ("us-gaap", "SalesRevenueNet"), ("us-gaap", "SalesRevenueGoodsNet")], "DURATION"),
    "operating_income": ("USD", [("us-gaap", "OperatingIncomeLoss")], "DURATION"),
    "net_income": ("USD", [("us-gaap", "NetIncomeLoss")], "DURATION"),
    "pretax_income": ("USD", [
        ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
        ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments")],
        "DURATION"),
    "income_tax": ("USD", [("us-gaap", "IncomeTaxExpenseBenefit")], "DURATION"),
    "cfo": ("USD", [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
                    ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")], "DURATION"),
    "capex": ("USD", [("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
                      ("us-gaap", "PaymentsToAcquireProductiveAssets")], "DURATION"),
    "dna": ("USD", [("us-gaap", "DepreciationDepletionAndAmortization"),
                    ("us-gaap", "DepreciationAndAmortization"),
                    ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
                    ("us-gaap", "DepreciationAmortizationAndOther"),
                    ("us-gaap", "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment")], "DURATION"),
    "sbc": ("USD", [("us-gaap", "ShareBasedCompensation"),
                    ("us-gaap", "AllocatedShareBasedCompensationExpense")], "DURATION"),
    "interest_expense": ("USD", [("us-gaap", "InterestExpense"), ("us-gaap", "InterestExpenseNonoperating"),
                                 ("us-gaap", "InterestExpenseDebt")], "DURATION"),
    "buybacks": ("USD", [("us-gaap", "PaymentsForRepurchaseOfCommonStock")], "DURATION"),
    "dividends_paid": ("USD", [("us-gaap", "PaymentsOfDividends"), ("us-gaap", "PaymentsOfDividendsCommonStock")],
                       "DURATION"),
    "shares_diluted_weighted": ("shares", [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding")], "DURATION"),
    "cash": ("USD", [("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
                     ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")], "INSTANT"),
    "short_term_investments": ("USD", [("us-gaap", "ShortTermInvestments"), ("us-gaap", "MarketableSecuritiesCurrent"),
                                       ("us-gaap", "AvailableForSaleSecuritiesDebtSecuritiesCurrent")], "INSTANT"),
    # Debt is modelled as separate concepts: the us-gaap tags below have DIFFERENT scopes and are never
    # substitutes for one another (see debt_total()).
    "long_term_debt_noncurrent": ("USD", [("us-gaap", "LongTermDebtNoncurrent")], "INSTANT"),
    "long_term_debt_total": ("USD", [("us-gaap", "LongTermDebt")], "INSTANT"),          # includes current maturities
    "long_term_debt_current": ("USD", [("us-gaap", "LongTermDebtCurrent")], "INSTANT"),  # current maturities only
    "debt_current": ("USD", [("us-gaap", "DebtCurrent")], "INSTANT"),                    # all current debt
    "short_term_borrowings": ("USD", [("us-gaap", "ShortTermBorrowings")], "INSTANT"),
    "commercial_paper": ("USD", [("us-gaap", "CommercialPaper")], "INSTANT"),           # often inside short-term borrowings
    "operating_lease_liabilities": ("USD", [("us-gaap", "OperatingLeaseLiability")], "INSTANT"),
    "minority_interest": ("USD", [("us-gaap", "MinorityInterest")], "INSTANT"),
    "total_equity": ("USD", [("us-gaap", "StockholdersEquity")], "INSTANT"),
    "total_assets": ("USD", [("us-gaap", "Assets")], "INSTANT"),
    "shares_outstanding": ("shares", [("dei", "EntityCommonStockSharesOutstanding"),
                                      ("us-gaap", "CommonStockSharesOutstanding")], "INSTANT"),
}
# Weighted averages and other non-additive durations must never be derived by YTD subtraction.
NON_ADDITIVE = {"shares_diluted_weighted"}
TAG_PRIORITY = {(c, tax, tag): i for c, (_, tags, _) in CONCEPTS.items() for i, (tax, tag) in enumerate(tags)}
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}


def duration_months(start: date, end: date) -> int:
    return round(((end - start).days + 1) / 30.4375)


# ------------------------------------------------------------------ ingestion
def ingest_companyfacts(app: App, issuer_id: str, raw: bytes, raw_object_id: str | None = None) -> dict:
    """Normalize a companyfacts JSON document into as-filed ``financial_fact`` rows."""
    doc = json.loads(raw)
    facts = doc.get("facts", {})
    # map accession -> public_at from the filing index when we have it
    acc_public = {r["accession_no"]: (r["public_at"], r["id"]) for r in all_rows(
        app.conn, "SELECT accession_no, public_at, id FROM source_document WHERE issuer_id=? AND provider='SEC_EDGAR'",
        (issuer_id,))}
    inserted, skipped_units, total = 0, 0, 0
    for concept, (unit_kind, tags, ptype) in CONCEPTS.items():
        for tax, tag in tags:
            node = facts.get(tax, {}).get(tag)
            if not node:
                continue
            units = node.get("units", {})
            if unit_kind not in units:
                skipped_units += sum(len(v) for v in units.values())
                continue
            for f in units[unit_kind]:
                form = f.get("form")
                if form not in FORMS:
                    continue
                total += 1
                end = date.fromisoformat(f["end"])
                start = date.fromisoformat(f["start"]) if f.get("start") else None
                if ptype == "DURATION" and start is None:
                    continue
                months = duration_months(start, end) if start else None
                if months is not None and months not in (3, 6, 9, 12):
                    continue
                acc = f.get("accn")
                filed = date.fromisoformat(f["filed"])
                pub, doc_id = acc_public.get(acc, (None, None))
                if pub is None:
                    pub = iso_utc(end_of_day_public(filed))
                inserted += insert(app.conn, "financial_fact", {
                    "id": new_id("fct"), "issuer_id": issuer_id, "concept": concept, "source_taxonomy": tax,
                    "source_tag": tag, "unit": unit_kind, "value": dstr(D(f["val"])), "null_reason": None,
                    "period_type": ptype, "period_start": start.isoformat() if start else None,
                    "period_end": end.isoformat(), "duration_months": months, "fiscal_year": f.get("fy"),
                    "fiscal_period": f.get("fp"), "derived": 0, "derivation_note": None, "form": form,
                    "accession_no": acc, "filed_date": filed.isoformat(), "public_at": pub, "document_id": doc_id,
                    "raw_object_id": raw_object_id, "normalizer_version": NORMALIZER_VERSION,
                    "recorded_at": app.now_iso(),
                }, or_ignore=True)
    return {"facts_seen": total, "inserted": inserted, "non_matching_unit_values": skipped_units}


def add_fact(app: App, issuer_id: str, concept: str, value, *, start: date | None, end: date, public_at: datetime,
             accession: str, form: str = "10-K", tag: str | None = None, unit: str | None = None,
             fiscal_year: int | None = None, fiscal_period: str | None = None, document_id: str | None = None) -> None:
    """Insert one fact (fixtures, manual entry, tests)."""
    unit_kind, tags, ptype = CONCEPTS[concept]
    insert(app.conn, "financial_fact", {
        "id": new_id("fct"), "issuer_id": issuer_id, "concept": concept, "source_taxonomy": (tags[0][0]),
        "source_tag": tag or tags[0][1], "unit": unit or unit_kind, "value": dstr(D(value)),
        "null_reason": None if value is not None else "not reported",
        "period_type": ptype, "period_start": start.isoformat() if start else None, "period_end": end.isoformat(),
        "duration_months": duration_months(start, end) if start else None, "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period, "derived": 0, "derivation_note": None, "form": form,
        "accession_no": accession, "filed_date": public_at.date().isoformat(), "public_at": iso_utc(public_at),
        "document_id": document_id, "raw_object_id": None, "normalizer_version": NORMALIZER_VERSION,
        "recorded_at": app.now_iso(),
    })


# ------------------------------------------------------------------ point-in-time view
@dataclass(frozen=True)
class FactValue:
    concept: str
    start: date | None
    end: date
    value: Decimal | None
    public_at: str
    accession: str | None
    source_tag: str | None
    derived: bool = False
    note: str | None = None
    fact_id: str | None = None

    @property
    def months(self) -> int | None:
        return duration_months(self.start, self.end) if self.start else None


class FactView:
    def __init__(self, app: App, issuer_id: str, as_of: datetime):
        self.app = app
        self.issuer_id = issuer_id
        self.as_of = as_of
        rows = all_rows(app.conn, "SELECT * FROM financial_fact WHERE issuer_id=? AND public_at<=? ORDER BY public_at",
                        (issuer_id, iso_utc(as_of)))
        best: dict[tuple, tuple] = {}
        for r in rows:
            key = (r["concept"], r["period_start"], r["period_end"])
            prio = TAG_PRIORITY.get((r["concept"], r["source_taxonomy"], r["source_tag"]), 99)
            rank = (-prio, r["public_at"])
            if key not in best or rank >= best[key][0]:
                best[key] = (rank, r)
        self._facts: dict[str, list[FactValue]] = {}
        for (concept, _s, _e), (_rank, r) in best.items():
            self._facts.setdefault(concept, []).append(FactValue(
                concept, date.fromisoformat(r["period_start"]) if r["period_start"] else None,
                date.fromisoformat(r["period_end"]), D(r["value"]), r["public_at"], r["accession_no"],
                r["source_tag"], False, None, r["id"]))
        for v in self._facts.values():
            v.sort(key=lambda f: (f.end, f.start or date.min))

    @property
    def latest_public_at(self) -> str | None:
        allf = [f.public_at for v in self._facts.values() for f in v]
        return max(allf) if allf else None

    def has(self, concept: str) -> bool:
        return bool(self._facts.get(concept))

    def durations(self, concept: str, months: int) -> list[FactValue]:
        return [f for f in self._facts.get(concept, []) if f.start and f.months == months]

    def annual(self, concept: str) -> list[FactValue]:
        """Fiscal-year values (12-month durations), oldest first; one per period end."""
        return self.durations(concept, 12)

    def instant(self, concept: str, on_or_before: date | None = None) -> FactValue | None:
        vals = [f for f in self._facts.get(concept, []) if f.start is None
                and (on_or_before is None or f.end <= on_or_before)]
        return vals[-1] if vals else None

    def instant_at(self, concept: str, d: date) -> FactValue | None:
        """Instant value reported for exactly period end ``d`` (no substitution from other dates)."""
        vals = [f for f in self._facts.get(concept, []) if f.start is None and f.end == d]
        return vals[-1] if vals else None

    def instant_dates(self, concepts: tuple[str, ...], on_or_before: date | None = None) -> list[date]:
        return sorted({f.end for c in concepts for f in self._facts.get(c, [])
                       if f.start is None and f.value is not None and (on_or_before is None or f.end <= on_or_before)},
                      reverse=True)

    def quarters(self, concept: str) -> list[FactValue]:
        """Discrete quarterly values, deriving from YTD durations where needed (additive concepts only)."""
        by_end: dict[date, FactValue] = {f.end: f for f in self.durations(concept, 3)}
        if concept in NON_ADDITIVE:
            return [by_end[k] for k in sorted(by_end)]
        ytd = {m: {f.end: f for f in self.durations(concept, m)} for m in (6, 9, 12)}
        # Walk YTD chains: a YTD value of m months ending at E with start S; the prior YTD shares start S.
        for m, prev_m in ((6, 3), (9, 6), (12, 9)):
            for end, f in ytd[m].items():
                if end in by_end:
                    continue
                prev = None
                if prev_m == 3:
                    prev = next((q for q in self.durations(concept, 3) if q.start == f.start), None)
                else:
                    prev = next((p for p in ytd[prev_m].values() if p.start == f.start), None)
                if prev is None or f.value is None or prev.value is None:
                    continue
                by_end[end] = FactValue(concept, prev.end + timedelta(days=1), end, f.value - prev.value,
                                        max(f.public_at, prev.public_at), f.accession, f.source_tag, True,
                                        f"{m}M YTD ({f.accession}) minus {prev_m}M ({prev.accession})")
        return [by_end[k] for k in sorted(by_end)]

    def ttm(self, concept: str) -> FactValue | None:
        if concept in NON_ADDITIVE:
            return None
        q = self.quarters(concept)
        if len(q) < 4:
            return None
        last4 = q[-4:]
        for a, b in zip(last4, last4[1:]):
            if b.start is None or abs((b.start - a.end).days - 1) > 7:
                return None   # not contiguous
        if any(x.value is None for x in last4):
            return None
        return FactValue(concept, last4[0].start, last4[-1].end, sum((x.value for x in last4), Decimal(0)),
                         max(x.public_at for x in last4), last4[-1].accession, last4[-1].source_tag, True,
                         "sum of last four quarters")

    def latest_period_end(self) -> date | None:
        ends = [f.end for f in self._facts.get("revenue", []) + self._facts.get("cfo", [])]
        return max(ends) if ends else None


def latest_financials_public_at(app: App, issuer_id: str) -> str | None:
    r = one(app.conn, "SELECT MAX(public_at) AS p FROM financial_fact WHERE issuer_id=?", (issuer_id,))
    return r["p"] if r else None


def as_of_dt(value: datetime | str) -> datetime:
    return parse_utc(value) if isinstance(value, str) else value  # type: ignore[return-value]



# ------------------------------------------------------------------ balance-sheet aggregates
DEBT_CONCEPTS = ("long_term_debt_noncurrent", "long_term_debt_total", "long_term_debt_current", "debt_current",
                 "short_term_borrowings", "commercial_paper")


@dataclass
class Aggregate:
    """A balance-sheet total built from same-date components.

    ``value`` covers only KNOWN components. ``missing`` lists components that were not reported for that date:
    the total is complete only when ``missing`` is empty. ``basis`` is FACT (one reported total), DERIVED (sum or
    difference of reported facts) or None (nothing reported). Missing parts are never silently zero.
    """
    name: str
    value: Decimal | None
    period_end: date | None
    method: str
    components: list[tuple[str, Decimal, str | None, str | None]]   # (concept, value, fact_id, accession)
    missing: list[str]
    warnings: list[str]
    basis: str | None

    @property
    def complete(self) -> bool:
        return self.value is not None and not self.missing

    @property
    def fact_ids(self) -> list[str]:
        return [c[2] for c in self.components if c[2]]


def debt_total(fv: FactView, on_or_before: date | None = None) -> Aggregate:
    """Total financial debt at the latest balance-sheet date with any debt concept, without double counting.

    Long-term part: LongTermDebtNoncurrent; else LongTermDebt - LongTermDebtCurrent; else LongTermDebt (which
    already includes current maturities). Current part: DebtCurrent (all current debt) when reported; otherwise
    current maturities + short-term borrowings (commercial paper only when short-term borrowings are absent,
    because CP is commonly inside them). Components from other dates are never mixed in.
    """
    dates = fv.instant_dates(DEBT_CONCEPTS, on_or_before)
    if not dates:
        return Aggregate("debt", None, None, "no debt concept reported", [], ["all debt concepts"], [], None)
    d = dates[0]
    g = {c: fv.instant_at(c, d) for c in DEBT_CONCEPTS}
    v = {c: (f.value if f and f.value is not None else None) for c, f in g.items()}
    comps: list[tuple[str, Decimal, str | None, str | None]] = []
    missing: list[str] = []
    warnings: list[str] = []
    method: list[str] = []

    def use(c: str, sign: int = 1) -> Decimal:
        comps.append((c if sign > 0 else f"-{c}", v[c] * sign, g[c].fact_id, g[c].accession))
        return v[c] * sign

    total = Decimal(0)
    long_includes_current = False
    if v["long_term_debt_noncurrent"] is not None:
        total += use("long_term_debt_noncurrent")
        method.append("LongTermDebtNoncurrent")
    elif v["long_term_debt_total"] is not None and v["long_term_debt_current"] is not None:
        total += use("long_term_debt_total") + use("long_term_debt_current", -1)
        method.append("LongTermDebt - LongTermDebtCurrent")
    elif v["long_term_debt_total"] is not None:
        total += use("long_term_debt_total")
        long_includes_current = True
        method.append("LongTermDebt (includes current maturities)")
    else:
        missing.append("long-term debt")
    st_key = "short_term_borrowings" if v["short_term_borrowings"] is not None else \
        ("commercial_paper" if v["commercial_paper"] is not None else None)
    if v["debt_current"] is not None:
        if long_includes_current:
            if v["long_term_debt_current"] is not None:
                total += use("debt_current") + use("long_term_debt_current", -1)
                method.append("+ DebtCurrent - LongTermDebtCurrent (maturities already in LongTermDebt)")
            elif st_key:
                total += use(st_key)
                method.append(f"+ {st_key} (DebtCurrent overlaps LongTermDebt; its split is unknown)")
                warnings.append("DebtCurrent reported but not separable from LongTermDebt; used short-term borrowings instead")
            else:
                missing.append("short-term borrowings (DebtCurrent overlaps LongTermDebt and cannot be separated)")
        else:
            total += use("debt_current")
            method.append("+ DebtCurrent")
    else:
        if not long_includes_current:
            if v["long_term_debt_current"] is not None:
                total += use("long_term_debt_current")
                method.append("+ LongTermDebtCurrent")
            else:
                missing.append("current maturities of long-term debt")
        if st_key:
            total += use(st_key)
            method.append(f"+ {st_key}")
        else:
            missing.append("short-term borrowings")
    if v["short_term_borrowings"] is not None and v["commercial_paper"] is not None:
        warnings.append("commercial paper not added separately (commonly included in short-term borrowings)")
    other_dates = [c for c in DEBT_CONCEPTS if g[c] is None and fv.instant(c, on_or_before) is not None]
    for c in other_dates:
        warnings.append(f"{c} reported only for other dates ({fv.instant(c, on_or_before).end}); not combined with {d}")
    basis = None if missing else ("FACT" if len(comps) == 1 else "DERIVED")
    return Aggregate("debt", total, d, " ".join(method), comps, missing, warnings, basis)


def cash_total(fv: FactView, on_or_before: date | None = None) -> Aggregate:
    dates = fv.instant_dates(("cash",), on_or_before)
    if not dates:
        return Aggregate("cash", None, None, "no cash concept reported", [], ["cash and equivalents"], [], None)
    d = dates[0]
    c, s = fv.instant_at("cash", d), fv.instant_at("short_term_investments", d)
    comps = [("cash", c.value, c.fact_id, c.accession)]
    missing, warnings = [], []
    total = c.value
    if s is not None and s.value is not None:
        comps.append(("short_term_investments", s.value, s.fact_id, s.accession))
        total += s.value
    else:
        missing.append("short-term investments")
        later = fv.instant("short_term_investments", on_or_before)
        if later is not None:
            warnings.append(f"short-term investments reported only for {later.end}; not combined with {d}")
    return Aggregate("cash", total, d, "cash" + (" + short-term investments" if len(comps) > 1 else ""), comps, missing,
                     warnings, "FACT" if len(comps) == 1 else "DERIVED")
