"""Return mathematics: time-weighted return, money-weighted return (IRR), drawdown.

IRR is reported only when it is mathematically well defined: we bracket every sign change of
NPV(r) on a wide grid and refuse to pick one when there are several roots.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass
class IRRResult:
    rate: float | None
    status: str            # SOLVED | NO_SOLUTION | MULTIPLE_SOLUTIONS | INSUFFICIENT_DATA
    roots: list[float]
    note: str


def _npv(rate: float, flows: list[tuple[float, float]]) -> float:
    return sum(a / (1.0 + rate) ** t for t, a in flows)


def irr_from_times(flows: list[tuple[float, float]]) -> IRRResult:
    flows = [(t, a) for t, a in flows if a != 0]
    if len(flows) < 2:
        return IRRResult(None, "INSUFFICIENT_DATA", [], "need at least two non-zero cash flows")
    if all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return IRRResult(None, "NO_SOLUTION", [], "all cash flows have the same sign")
    grid = [-0.99 + i * 0.01 for i in range(0, 99)] + [x / 10 for x in range(0, 101)] + [10.0 + x for x in range(1, 91)]
    grid = sorted(set(round(g, 6) for g in grid))
    roots: list[float] = []
    prev_r, prev_v = grid[0], _npv(grid[0], flows)
    for r in grid[1:]:
        v = _npv(r, flows)
        if prev_v == 0:
            roots.append(prev_r)
        elif (prev_v < 0) != (v < 0) and not (math.isinf(v) or math.isinf(prev_v)):
            lo, hi, flo = prev_r, r, prev_v
            for _ in range(200):
                mid = (lo + hi) / 2
                fm = _npv(mid, flows)
                if (fm < 0) == (flo < 0):
                    lo, flo = mid, fm
                else:
                    hi = mid
                if hi - lo < 1e-12:
                    break
            roots.append((lo + hi) / 2)
        prev_r, prev_v = r, v
    roots = sorted(set(round(x, 10) for x in roots))
    if not roots:
        return IRRResult(None, "NO_SOLUTION", [], "NPV has no sign change between -99% and 10000%")
    if len(roots) > 1:
        return IRRResult(None, "MULTIPLE_SOLUTIONS", roots, "cash-flow signs change more than once; IRR ambiguous")
    return IRRResult(roots[0], "SOLVED", roots, "annualized")


def xirr(flows: list[tuple[date, Decimal]]) -> IRRResult:
    """Money-weighted return. Flows from the investor's view: contributions negative, value/withdrawals positive."""
    if not flows:
        return IRRResult(None, "INSUFFICIENT_DATA", [], "no flows")
    t0 = min(d for d, _ in flows)
    return irr_from_times([((d - t0).days / 365.25, float(a)) for d, a in flows])


def time_weighted_return(navs: list[tuple[date, Decimal]], flows: dict[date, Decimal]) -> tuple[Decimal | None, list[tuple[date, Decimal]]]:
    """Chain-linked daily TWR.

    Convention: external flows on day d arrive at the START of day d (before that day's market move),
    so r_d = NAV_d / (NAV_{d-1} + F_d) - 1. Returns (total return, cumulative index series).
    """
    if len(navs) < 2:
        return None, []
    idx = Decimal(1)
    series = [(navs[0][0], idx)]
    for (d0, v0), (d1, v1) in zip(navs, navs[1:]):
        base = v0 + flows.get(d1, Decimal(0))
        if base <= 0:
            return None, series   # undefined when the capital base is zero or negative
        idx *= v1 / base
        series.append((d1, idx))
    return idx - 1, series


def max_drawdown(index: list[tuple[date, Decimal]]) -> tuple[Decimal | None, date | None, date | None]:
    if not index:
        return None, None, None
    peak, peak_d = index[0][1], index[0][0]
    worst, w_start, w_end = Decimal(0), None, None
    for d, v in index:
        if v > peak:
            peak, peak_d = v, d
        dd = v / peak - 1
        if dd < worst:
            worst, w_start, w_end = dd, peak_d, d
    return worst, w_start, w_end
