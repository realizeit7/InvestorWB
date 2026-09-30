"""Deterministic evaluation of METRIC milestones and invalidation conditions.

Metric keys are ``<concept>:<metric>`` where metric is
  value       the reported fiscal-year (or TTM) value
  margin      concept / revenue for the same period
  growth_yoy  value / prior-year value - 1
Only METRIC conditions are evaluated by code (and can therefore be VERIFIED). EVENT/JUDGMENT
conditions need an owner assessment; an LLM suggestion is recorded as unverified.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .fundamentals import FactView

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}


def metric_series(fv: FactView, key: str, basis: str = "FY") -> list[tuple[date, Decimal, list[str]]]:
    concept, _, metric = key.partition(":")
    metric = metric or "value"
    if basis == "TTM":
        cur = fv.ttm(concept)
        if cur is None:
            return []
        if metric == "margin":
            rev = fv.ttm("revenue")
            if rev is None or not rev.value:
                return []
            return [(cur.end, cur.value / rev.value, [cur.accession or "", rev.accession or ""])]
        return [(cur.end, cur.value, [cur.accession or ""])]
    vals = [f for f in fv.annual(concept) if f.value is not None]
    if metric == "value":
        return [(f.end, f.value, [f.accession or ""]) for f in vals]
    if metric == "margin":
        rev = {f.end: f for f in fv.annual("revenue") if f.value}
        return [(f.end, f.value / rev[f.end].value, [f.accession or "", rev[f.end].accession or ""])
                for f in vals if f.end in rev]
    if metric == "growth_yoy":
        out = []
        for a, b in zip(vals, vals[1:]):
            if a.value and a.value > 0 and 330 <= (b.end - a.end).days <= 400:
                out.append((b.end, b.value / a.value - 1, [a.accession or "", b.accession or ""]))
        return out
    raise ValueError(f"unknown metric {metric}")


def evaluate_condition(fv: FactView, cond: dict) -> tuple[str, dict]:
    """Returns (state, evidence) for an invalidation condition row."""
    if cond["kind"] != "METRIC":
        return "UNKNOWN", {"reason": f"{cond['kind']} condition needs owner assessment"}
    if not cond["concept"] or not cond["comparator"] or cond["threshold"] is None:
        return "UNKNOWN", {"reason": "metric condition incompletely specified"}
    k = cond["consecutive_periods"] or 1
    series = metric_series(fv, cond["concept"], cond["period_basis"] or "FY")
    if not series:
        return "UNKNOWN", {"reason": "no data for this metric (missing evidence)"}
    thr = Decimal(cond["threshold"])
    last = series[-k:]
    hits = [OPS[cond["comparator"]](v, thr) for _, v, _ in last]
    ev = {"periods": [{"end": d.isoformat(), "value": str(v), "sources": s} for d, v, s in last],
          "rule": f"{cond['concept']} {cond['comparator']} {thr} for {k} period(s)",
          "latest_breaches": bool(hits and hits[-1])}
    if len(series) < k:
        ev["reason"] = f"only {len(series)} of {k} required periods available; cannot be triggered yet"
        return "NOT_TRIGGERED", ev
    return ("TRIGGERED" if all(hits) else "NOT_TRIGGERED"), ev


def evaluate_milestone(fv: FactView, ms: dict, today: date) -> tuple[str, dict]:
    if not ms["concept"] or not ms["comparator"] or ms["target"] is None:
        return "PENDING", {"reason": "qualitative milestone: owner assessment required"}
    series = metric_series(fv, ms["concept"], "FY")
    due = date.fromisoformat(ms["due_date"]) if ms["due_date"] else None
    relevant = [s for s in series if due is None or s[0] <= due]
    if relevant and OPS[ms["comparator"]](relevant[-1][1], Decimal(ms["target"])):
        d, v, src = relevant[-1]
        return "MET", {"end": d.isoformat(), "value": str(v), "sources": src}
    if due and today > due:
        last = relevant[-1] if relevant else None
        return "MISSED", {"due": due.isoformat(), "last": None if last is None else {"end": last[0].isoformat(), "value": str(last[1])}}
    return "PENDING", {"due": due.isoformat() if due else None}
