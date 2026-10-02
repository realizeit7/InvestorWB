"""Company finder (discovery) — offline: synthetic Nasdaq rows, SEC map and frames; deep stage on FIXTURE companies."""

import json
from decimal import Decimal as D

import pytest

from equity_monitor.data.securities import find_security
from equity_monitor.fixtures import AS_OF, build_demo
from equity_monitor.research.finder import build_universe, prelim_metrics, run_finder, shortlist, UniverseRow

DEMO = ["ZZADD", "ZZHLD", "ZZTRM", "ZZEXT", "ZZREV", "ZZNEW"]


def _nasdaq(sym, cap="5000000000", px="$30.00", vol="500000", sector="Technology", country="United States", name=None):
    return {"symbol": sym, "name": name or f"{sym} Common Stock", "lastsale": px, "marketCap": cap, "volume": vol,
            "sector": sector, "industry": "x", "country": country}


def _universe_inputs():
    nasdaq = [_nasdaq(s) for s in DEMO] + [
        _nasdaq("ZZBNK", sector="Finance"), _nasdaq("ZZSML", cap="200000000"), _nasdaq("ZZILQ", vol="1000"),
        _nasdaq("ZZFOR", country="Netherlands"), _nasdaq("ZZADD/W", name="Addco Warrants"), _nasdaq("ZZNOCIK"),
        _nasdaq("ZZADD.B", vol="100000"), _nasdaq("ZZOTC")]
    sec_map = [{"cik": 99000000 + i, "name": s, "ticker": s, "exchange": "Nasdaq"} for i, s in enumerate(DEMO)] + [
        {"cik": 1, "name": "bank", "ticker": "ZZBNK", "exchange": "NYSE"}, {"cik": 2, "name": "s", "ticker": "ZZSML", "exchange": "NYSE"},
        {"cik": 3, "name": "i", "ticker": "ZZILQ", "exchange": "NYSE"}, {"cik": 4, "name": "f", "ticker": "ZZFOR", "exchange": "NYSE"},
        {"cik": 99000000, "name": "ZZADD", "ticker": "ZZADD-B", "exchange": "Nasdaq"},
        {"cik": 5, "name": "o", "ticker": "ZZOTC", "exchange": "OTC"}]
    return nasdaq, sec_map


def _frames(universe_ciks, years):
    """Revenue/OI/CFO/capex frames: higher margins and growth for lower CIK index."""
    fr = {}
    for k, y in enumerate(sorted(years)):
        for c in ("revenue", "operating_income", "cfo", "capex"):
            fr[(c, y)] = {}
        for j, cik in enumerate(universe_ciks):
            rev = D(1000) * (D("1.0") + D(k) * D("0.05") * (6 - j))
            fr[("revenue", y)][cik] = rev
            fr[("operating_income", y)][cik] = rev * (D("0.30") - D(j) * D("0.03"))
            fr[("cfo", y)][cik] = rev * D("0.25")
            fr[("capex", y)][cik] = rev * D("0.05")
    return fr


def test_universe_filters_are_explicit(app):
    nasdaq, sec_map = _universe_inputs()
    uni, dropped = build_universe(app, nasdaq, sec_map)
    assert [u.symbol for u in uni] == sorted(DEMO)
    assert dropped == {"excluded sector": 1, "market cap below floor": 1, "dollar volume below floor": 1,
                       "country (non-US filer)": 1, "not common stock": 1, "no SEC CIK": 1,
                       "other share class of the same company": 1, "exchange": 1}


def test_prelim_metrics_never_treat_missing_as_zero(app):
    u = UniverseRow("X", 7, "x", "NYSE", "Technology", None, "United States", D(10**9), D(10), D(10**7))
    m, latest = prelim_metrics(u, {("revenue", 2025): {7: D(100)}}, [2025, 2024, 2023, 2022, 2021])
    assert latest == 2025 and all(v is None for v in m.values())                # only revenue known: nothing invented
    m2, _ = prelim_metrics(u, {}, [2025])
    assert m2 == {k: None for k in m2}


def test_finder_run_records_shortlist_from_point_in_time_deep_scores(app):
    app.clock.set(AS_OF)
    build_demo(app)
    nasdaq, sec_map = _universe_inputs()
    years = [AS_OF.year - k for k in range(1, 6)]
    frames = _frames([99000000 + i for i in range(6)], years)
    fetched = []

    def deep_fetch(u):
        fetched.append(u.symbol)
        return find_security(app.conn, u.symbol)
    rid = run_finder(app, nasdaq_rows=nasdaq, sec_map=sec_map, frames=frames, deep_fetch=deep_fetch, as_of=AS_OF)
    run = app.conn.execute("SELECT * FROM finder_run WHERE id=?", (rid,)).fetchone()
    assert run["universe_count"] == 6 and run["label"] == "CURRENT"
    assert sorted(fetched) == sorted(DEMO)
    sl = shortlist(app, rid)
    assert sl and [c["rank"] for c in sl] == list(range(1, len(sl) + 1))
    for c in sl:
        assert c["security_id"] and set(c["scores"]) >= {"quality", "value"}
        assert c["metrics"].get("dcf_base_value_per_share") is not None
    deep = {r["symbol"]: r for r in app.conn.execute("SELECT * FROM finder_candidate WHERE run_id=? AND stage='DEEP'", (rid,))}
    assert set(deep) == set(DEMO)
    # nothing in the finder creates recommendations, watchlist entries or decisions
    assert app.conn.execute("SELECT COUNT(*) FROM recommendation").fetchone()[0] == 0
    assert app.conn.execute("SELECT COUNT(*) FROM watchlist_entry WHERE status='RESEARCH'").fetchone()[0] == 0
    with pytest.raises(Exception):
        app.conn.execute("DELETE FROM finder_candidate")


def test_failed_deep_fetch_is_a_warning_not_a_crash(app):
    app.clock.set(AS_OF)
    build_demo(app)
    nasdaq, sec_map = _universe_inputs()
    years = [AS_OF.year - k for k in range(1, 6)]
    frames = _frames([99000000 + i for i in range(6)], years)

    def deep_fetch(u):
        if u.symbol == "ZZADD":
            raise RuntimeError("SEC timeout")
        return find_security(app.conn, u.symbol)
    rid = run_finder(app, nasdaq_rows=nasdaq, sec_map=sec_map, frames=frames, deep_fetch=deep_fetch, as_of=AS_OF)
    w = json.loads(app.conn.execute("SELECT warnings_json FROM finder_run WHERE id=?", (rid,)).fetchone()[0])
    assert any("ZZADD" in x and "SEC timeout" in x for x in w)
    assert "ZZADD" not in {c["symbol"] for c in shortlist(app, rid)}
