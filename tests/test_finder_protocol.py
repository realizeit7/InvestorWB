"""Review of 77a3ad1: tied ranks, evaluation timing and frozen cohorts, Claude Code isolation, spec decisions.
Offline and credential-free (the real-CLI isolation test uses a local fake API, a dummy key and a temporary home)."""

import json
import os
import random
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from equity_monitor.data import calendar as cal
from equity_monitor.data.prices import Action, Bar, PriceFetch, store_fetch
from equity_monitor.data.securities import find_security, register_security
from equity_monitor.db.core import insert
from equity_monitor.fixtures import AS_OF
from equity_monitor.research.finder import (assign_peers, classification_conflict, peer_levels, prelim_rank,
                                            shortlist, UniverseRow)
from equity_monitor.research.finder_eval import entry_session, evaluate, open_to_open, summarize
from equity_monitor.research.screening import percentile_ranks

from test_finder import FLAGS_HELP, _fake_claude, _judgment, _run, clean_env  # noqa: F401  (fixture re-export)

UTC = timezone.utc


# ================================================================== 1. tied metrics get equal ranks
def test_equal_metrics_get_equal_percentiles_whatever_the_input_order():
    vals = [("A", D(3)), ("B", D(3)), ("C", D(3))]
    assert percentile_ranks(vals, 1) == {"A": D("0.5"), "B": D("0.5"), "C": D("0.5")}     # was 0, 0.5, 1
    assert percentile_ranks(list(reversed(vals)), 1) == percentile_ranks(vals, 1)
    mixed = [("A", D(1)), ("B", D(5)), ("C", D(5)), ("D", D(9))]
    assert percentile_ranks(mixed, 1) == {"A": D(0), "B": D("0.5"), "C": D("0.5"), "D": D(1)}
    assert percentile_ranks(mixed, -1)["A"] == D(1)


def test_prelim_scores_do_not_depend_on_input_order(app):
    rows = []
    for i in range(30):
        u = UniverseRow(f"S{i:02d}", 1000 + i, f"s{i}", "NYSE", "Technology", None, "United States", D(10**9), D(10), D(10**7))
        m = {"revenue_cagr_3y": D(i % 4) / 10, "op_margin": D(i % 3) / 10, "op_margin_trend": None,
             "fcf_margin_avg": D("0.1"), "fcf_yield": D(i % 2) / 100, "fcf_positive_years": D(3)}   # many exact ties
        rows.append((u, m))
    base = {u.symbol: s for u, _m, s, _c in prelim_rank(app, rows)}
    for seed in range(5):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert {u.symbol: s for u, _m, s, _c in prelim_rank(app, shuffled)} == base
    same = [s for (u, m), (_u, _m, s, _c) in zip(rows, prelim_rank(app, rows)) if u.symbol in ("S00", "S12")]
    assert same[0] == same[1]                                       # identical metrics => identical score


# ================================================================== 4. peer groups (SIC hierarchy) + conflicts
def test_sic_peer_groups_use_the_finest_populated_level_then_fallback():
    assert peer_levels("7372") == ["SIC4:7372", "SIC3:737", "SIC2:73", "DIV:I"] and peer_levels(None) == []
    sics = {f"sw{i}": "7372" for i in range(5)} | {f"it{i}": "7370" for i in range(2)} | {"bank": "6022", "x": None}
    lab = assign_peers(sics, 5)
    assert lab["sw0"] == "SIC4:7372"                                # 5 members at the 4-digit level
    assert lab["it0"] == "SIC3:737"                                 # rolls up to the 3-digit group (7 members)
    assert lab["bank"] == "ALL" and lab["x"] == "ALL"               # too small everywhere / SIC unknown: last resort
    assert classification_conflict("Industrials", "7372") is None
    assert "Health Care" in classification_conflict("Health Care", "6022")
    assert classification_conflict("Miscellaneous", "6022") is None  # no expectation for this label


# ================================================================== 2. evaluation timing and frozen cohorts
def test_cohort_entry_is_the_first_open_after_the_information_existed():
    wed = date(2026, 9, 30)
    assert entry_session(datetime(2026, 9, 30, 12, 0, tzinfo=UTC)) == wed                 # before the 13:30Z open
    assert entry_session(datetime(2026, 9, 30, 15, 0, tzinfo=UTC)) == date(2026, 10, 1)  # open already passed
    assert entry_session(datetime(2026, 9, 30, 22, 0, tzinfo=UTC)) == date(2026, 10, 1)  # after the close
    assert entry_session(datetime(2026, 10, 4, 14, 0, tzinfo=UTC)) == date(2026, 10, 5)  # Sunday shortlist -> Monday


def _bars(app, sid, days, start=D(100), drift=D("0.002"), gap=D("0.01"), volume=D(10**6)):
    """Open and close differ, so open-to-open and close-to-close returns are distinguishable."""
    bars, px = [], start
    for d in days:
        bars.append(Bar(d, px * (1 + gap), open=px, volume=volume))
        px = px * (1 + drift)
    store_fetch(app, sid, PriceFetch(bars), "fixture_eval")


def _sessions(start: date, n: int) -> list[date]:
    out = [cal.session_on_or_after(start)]
    while len(out) < n:
        out.append(cal.next_session(out[-1]))
    return out


def test_open_to_open_return_includes_dividends_and_splits(app):
    sid = register_security(app.conn, app.now_iso(), "ZZDIV", security_type="COMMON")
    days = _sessions(date(2026, 1, 5), 10)
    bars = [Bar(d, D(101), open=D(100)) for d in days[:5]] + [Bar(d, D("50.5"), open=D(50)) for d in days[5:]]
    store_fetch(app, sid, PriceFetch(bars, [Action("SPLIT", days[5], D(2), D(1)),
                                           Action("CASH_DIVIDEND", days[2], cash_amount=D("1.01"))]), "fixture")
    r, why, path = open_to_open(app, sid, days[0], days[9])
    # 1 unit -> +1% via dividend reinvested at the ex-date close (1.01/101) -> x2 split; exit open 50
    assert why is None and abs(r - float(D("1.01") * 2 * 50 / 100 - 1)) < 1e-12 and path[-1][0] == days[9]
    r2, why2, _ = open_to_open(app, sid, days[0], cal.next_session(days[9]))
    assert r2 is None and "delisted" in why2                                  # no exit price: unknown, never zero


def _with_cohort_prices(app, rid, days):
    spy = register_security(app.conn, app.now_iso(), "SPY", security_type="ETF")
    _bars(app, spy, days, drift=D("0.001"))
    for k, c in enumerate(shortlist(app, rid)):
        _bars(app, c["security_id"], days, drift=D("0.003") - D(k) * D("0.0005"))
    return spy


def test_later_judgments_never_change_a_matured_cohort(app, tmp_path):
    """Regression (review of 77a3ad1): a judgment added 500 days after a run changed that run's already-matured
    RESEARCH_FURTHER subgroup. Now the LLM arm is a cohort frozen when its judgments existed."""
    from equity_monitor.research.finder_judge import import_judgments
    rid = _run(app)
    sl = shortlist(app, rid)
    days = _sessions(cal.ny_date(AS_OF), 420)
    _with_cohort_prices(app, rid, days)
    app.clock.set(AS_OF + timedelta(hours=1))
    path = tmp_path / "j.json"
    path.write_text(json.dumps({"run_id": rid, "judgments": [_judgment(sl[0]["symbol"]), _judgment(sl[1]["symbol"])]}))
    first = import_judgments(app, path)["llm_cohort"]
    app.clock.set(AS_OF + timedelta(days=400))
    before = [a for p in evaluate(app)["protocols"] for a in p["arms"] if a["arm"] == "D_LLM_RULE"]
    d63 = next(a for a in before if a["horizon_sessions"] == 63)
    assert d63["complete_cohorts"] == 1 and d63["mean_net_excess_vs_spy"] is not None     # a real matured result
    app.clock.set(AS_OF + timedelta(days=500))
    path.write_text(json.dumps({"run_id": rid, "judgments": [_judgment(sl[2]["symbol"]),
                                                             _judgment(sl[0]["symbol"], "LIKELY_VALUE_TRAP")]}))
    second = import_judgments(app, path)["llm_cohort"]
    assert first and second and first != second
    rows = app.conn.execute("SELECT cohort_no, info_time, members_json FROM finder_cohort WHERE run_id=? AND "
                            "arm='D_LLM_RULE' ORDER BY cohort_no", (rid,)).fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert [m["symbol"] for m in json.loads(rows[0][2])] == [sl[0]["symbol"], sl[1]["symbol"]]   # frozen
    after_eval = evaluate(app)
    after = [a for p in after_eval["protocols"] for a in p["arms"] if a["arm"] == "D_LLM_RULE"]
    strip = lambda xs: [{k: v for k, v in a.items() if k != "cohorts"} for a in xs]               # noqa: E731
    assert strip(after) == strip(before)                                       # matured results unchanged
    assert next(p for p in after_eval["protocols"] if p["is_current"])["later_llm_refreezes_not_in_statistics"] == 1
    with pytest.raises(Exception):
        app.conn.execute("UPDATE finder_cohort SET members_json='[]'")


def test_arms_start_at_a_common_executable_price_and_spy_matches_the_interval(app):
    rid = _run(app)
    days = _sessions(cal.ny_date(AS_OF) - timedelta(days=400), 700)
    _with_cohort_prices(app, rid, days)
    arms = {r[0]: r for r in app.conn.execute("SELECT arm, info_time FROM finder_cohort WHERE run_id=?", (rid,))}
    assert set(arms) == {"A_QUALITY_VALUE", "B_RAW_GAP", "C_CONSERVATIVE_GAP"}          # D only after judgments
    assert len({r[1] for r in arms.values()}) == 1                               # same information time for A/B/C
    app.clock.set(AS_OF + timedelta(days=200))
    rep = evaluate(app, include_rows=True)
    cur = next(p for p in rep["protocols"] if p["is_current"])
    b63 = next(a for a in cur["arms"] if a["arm"] == "B_RAW_GAP" and a["horizon_sessions"] == 63)
    row = b63["rows"][0]
    # the run was recorded Wed 2026-09-30 after the close: entry is Thursday's OPEN, not Wednesday's close
    assert row["entry"] == "2026-10-01" and row["status"] == "COMPLETE"
    spy = find_security(app.conn, "SPY")
    from equity_monitor.research.finder_eval import add_sessions
    exp_spy, _, _ = open_to_open(app, spy, date(2026, 10, 1), add_sessions(date(2026, 10, 1), 63))
    assert row["spy_return"] == exp_spy and row["exit"] == add_sessions(date(2026, 10, 1), 63).isoformat()
    cost = float(app.policy.finder.evaluation.cost_bps_per_side) / 10000
    members = json.loads(app.conn.execute("SELECT members_json FROM finder_cohort WHERE run_id=? AND arm='B_RAW_GAP'",
                                          (rid,)).fetchone()[0])
    nets = [(1 - cost) ** 2 * (1 + open_to_open(app, m["security_id"], date(2026, 10, 1),
                                                date.fromisoformat(row["exit"]))[0]) - 1 for m in members]
    assert abs(row["net_return"] - sum(nets) / len(nets)) < 1e-12 and row["net_return"] < row["gross_return"]
    assert abs(row["excess_net_vs_spy"] - (row["net_return"] - exp_spy)) < 1e-12
    assert b63["verdict"] == "INSUFFICIENT_DATA" and "52" in b63["why"]
    assert rep["label"].startswith("HYPOTHETICAL") and "Not traded" in rep["label"]


def test_missing_outcomes_make_a_cohort_incomplete_not_a_smaller_cohort(app):
    rid = _run(app)
    days = _sessions(cal.ny_date(AS_OF) - timedelta(days=10), 120)
    _with_cohort_prices(app, rid, days)
    gone = shortlist(app, rid)[-1]["security_id"]                     # "delisted": no bars after 30 sessions
    app.conn.execute("DELETE FROM price_bar WHERE security_id=? AND session_date>?", (gone, days[30].isoformat()))
    app.clock.set(AS_OF + timedelta(days=150))
    cur = next(p for p in evaluate(app, include_rows=True)["protocols"] if p["is_current"])
    b = next(a for a in cur["arms"] if a["arm"] == "B_RAW_GAP" and a["horizon_sessions"] == 63)
    row = b["rows"][0]
    assert row["status"] == "INCOMPLETE" and row["coverage"] < 1 and row["missing"][0]["reason"].startswith("no opening price at exit")
    assert "net_return" not in row and "not the cohort's result" in row["note"]
    assert b["cohorts"]["INCOMPLETE"] == 1 and b["missing_member_outcomes"] == 1 and b["verdict"] == "INSUFFICIENT_DATA"


def _rows(xs, arm="B_RAW_GAP", contrib_keys=10, start=date(2024, 1, 1)):
    out = []
    for i, x in enumerate(xs):
        e = start + timedelta(days=7 * i)
        out.append({"status": "COMPLETE", "run_id": f"r{i}", "entry": e.isoformat(), "excess_net_vs_spy": x,
                    "net_return": x + 0.05, "spy_return": 0.05, "max_drawdown": -0.1,
                    "contributions": {f"s{(i + k) % contrib_keys}": x / 5 for k in range(5)}})
    return out


def test_verdict_gate_needs_predeclared_observations_baselines_and_dispersion(app):
    rng = random.Random(1)
    good = _rows([0.02 + rng.uniform(-0.01, 0.01) for _ in range(120)], contrib_keys=40)
    base = _rows([0.0 + rng.uniform(-0.01, 0.01) for _ in range(120)], contrib_keys=40)
    assert summarize(app, good[:30], "B_RAW_GAP", 126, base)["verdict"] == "INSUFFICIENT_DATA"     # < 52 cohorts
    s = summarize(app, good, "B_RAW_GAP", 126, base)
    assert s["verdict"] == "PROMISING" and s["improvement_over_baseline"]["paired_cohorts"] == 120
    assert summarize(app, good, "B_RAW_GAP", 126, good)["verdict"] == "NOT_SUPPORTED"           # no improvement
    neg = _rows([-0.01] * 120, contrib_keys=40)
    assert summarize(app, neg, "A_QUALITY_VALUE", 126, None)["verdict"] == "NOT_SUPPORTED"
    few = _rows([0.02 + rng.uniform(-0.01, 0.01) for _ in range(120)], contrib_keys=5)   # same 5 names every time
    assert summarize(app, few, "B_RAW_GAP", 126, base)["verdict"] == "INCONCLUSIVE"
    noisy = _rows([0.002 + rng.uniform(-0.2, 0.2) for _ in range(120)], contrib_keys=40)
    assert summarize(app, noisy, "B_RAW_GAP", 126, base)["verdict"] in ("INCONCLUSIVE", "NOT_SUPPORTED")


# ================================================================== spec: conservative gap, sensitivity, liquidity
def _small_peer_policy(app):
    f = app.policy.finder
    app.policy = app.policy.model_copy(update={"finder": f.model_copy(update={
        "conservative_gap": f.conservative_gap.model_copy(update={"min_peer_count": 3})})})


def test_conservative_gap_is_versioned_shrunk_capped_and_sensitivity_flagged(app):
    _small_peer_policy(app)
    rid = _run(app)
    cg = app.policy.finder.conservative_gap
    deep = [json.loads(r[0]) for r in app.conn.execute("SELECT metrics_json FROM finder_candidate WHERE run_id=? AND "
                                                       "stage='DEEP'", (rid,))]
    checked = 0
    for m in deep:
        if m.get("conservative_gap") is None:
            continue
        h, med, g = D(m["revenue_cagr_3y"]), D(m["peer_median_growth"]), D(m["conservative_growth"])
        assert g == min(cg.shrink_weight * h + (1 - cg.shrink_weight) * med, med + cg.max_excess_over_peer_median)
        assert D(m["conservative_gap"]) == g - D(m["implied_revenue_growth"])
        assert set(m["conservative_gap_sensitivity"]) == {"wacc_up", "terminal_growth_down", "margins_down", "dilution"}
        assert isinstance(m["fragile"], bool)
        checked += 1
    assert checked >= 3
    rule = json.loads(app.conn.execute("SELECT rule_json FROM finder_cohort WHERE run_id=? AND arm='C_CONSERVATIVE_GAP'",
                                       (rid,)).fetchone()[0])
    assert rule["conservative_gap"]["version"] == "cg-1"
    a = json.loads(app.conn.execute("SELECT rule_json FROM finder_cohort WHERE run_id=? AND arm='A_QUALITY_VALUE'",
                                    (rid,)).fetchone()[0])["weights"]
    assert set(a) == {"quality", "value"}                                    # renormalized quality + value only


def test_trailing_liquidity_replaces_the_single_session_proxy_in_the_deep_dive(app):
    from equity_monitor.research.finder import deep_score
    rid = _run(app)
    sid = find_security(app.conn, "ZZADD")
    days = [d for d in _sessions(cal.ny_date(AS_OF) - timedelta(days=40), 40) if d <= cal.latest_completed_session(AS_OF)]
    store_fetch(app, sid, PriceFetch([Bar(d, D(30), open=D(30), volume=D(100)) for d in days]), "aaa_thin")
    res = {r.symbol: r for r in deep_score(app, [("ZZADD", sid)], AS_OF)}
    assert res["ZZADD"].exclusion.startswith("LIQUIDITY<") and "20-session median" in res["ZZADD"].exclusion
    assert res["ZZADD"].metrics["trailing_median_dollar_volume"] == D(3000)


def test_report_highlights_top_priorities_and_states_the_narrow_scope(app):
    from equity_monitor.reporting.reports import finder_md
    rid = _run(app)
    md = finder_md(app, rid)
    assert "US country label listed on NYSE or Nasdaq" in md and "SINGLE-SESSION" in md
    assert "## Research first (" in md and "not a recommendation to own 25 stocks" in md
    assert "not a forecast of excess returns" in md and "Conservative gap (cg-1)" in md


# ================================================================== 3. Claude Code isolation and authentication
def test_preflight_refuses_ambiguous_or_unsupported_configurations(tmp_path, monkeypatch, clean_env):
    from equity_monitor.llm.claude_code_provider import preflight
    ok_script, _ = _fake_claude(tmp_path, {})
    assert preflight(str(ok_script), ["claude.ai"]).ok
    cases = [({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}, "auth method 'api_key'"),
             ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "apiKeySource": "apiKeyHelper"},
              "API key source"),
             ({"loggedIn": False}, "not logged in"),
             ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"}, "firstParty")]
    for i, (auth, msg) in enumerate(cases):
        d = tmp_path / f"c{i}"
        d.mkdir()
        s, _ = _fake_claude(d, {}, auth=auth)
        pf = preflight(str(s), ["claude.ai"])
        assert not pf.ok and any(msg in r for r in pf.reasons), (auth, pf.reasons)
    d = tmp_path / "old"
    d.mkdir()
    s, _ = _fake_claude(d, {}, help_text=FLAGS_HELP.replace("--safe-mode ", ""))
    assert any("--safe-mode" in r for r in preflight(str(s), ["claude.ai"]).reasons)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    assert any("CLAUDE_CODE_USE_BEDROCK" in r for r in preflight(str(ok_script), ["claude.ai"]).reasons)


def test_cli_judging_is_refused_until_the_isolation_check_passed_for_this_version(app, tmp_path, clean_env):
    from equity_monitor.llm.claude_code_check import run_check, validated
    from equity_monitor.llm.claude_code_provider import ClaudeCodeProvider
    from equity_monitor.research.finder_judge import JudgingRefused, judge_run
    rid = _run(app)
    script, log = _fake_claude(tmp_path, {"type": "result", "subtype": "success", "is_error": False,
                                          "structured_output": _judgment("ZZADD")})
    prov = ClaudeCodeProvider(str(script))
    with pytest.raises(JudgingRefused, match="claude-check"):
        judge_run(app, prov, rid, fetch_text=False)
    assert not log.exists()                                          # nothing was launched
    s = app.settings.llm.model_copy(update={"claude_code_bin": str(script)})
    app.settings = app.settings.model_copy(update={"llm": s})
    bad = run_check(app, isolation=lambda b: {"problems": ["isolated run executed user/project hooks"]})
    assert bad["status"] == "FAIL" and not validated(app, "2.1.287")[0]
    good = run_check(app, isolation=lambda b: {"problems": []})
    assert good["status"] == "PASS" and validated(app, "2.1.287")[0] and not validated(app, "2.1.288")[0]
    assert "email" not in json.dumps(good)                           # only non-secret auth fields are kept
    out = judge_run(app, ClaudeCodeProvider(str(script)), rid, fetch_text=False)
    assert "ZZADD" in out["judged"] or out["failed"]                  # launched only now


def test_weekly_job_falls_back_to_the_pack_when_cli_judging_is_not_validated(app, tmp_path, clean_env, monkeypatch):
    from equity_monitor.monitoring import jobs
    from equity_monitor.research import finder as fd
    rid = _run(app)
    script, log = _fake_claude(tmp_path, {})
    s = app.settings.llm.model_copy(update={"provider": "claude_code", "claude_code_bin": str(script)})
    app.settings = app.settings.model_copy(update={"llm": s, "finder_enabled": True, "finder_auto_judge": True})
    monkeypatch.setattr(fd, "run_finder", lambda app: rid)
    detail = jobs.weekly_finder(app, "job", AS_OF)
    assert "claude-check" in detail["judgment_refused"] and "pack" in detail and not log.exists()


def test_subscription_call_slots_are_reserved_atomically_across_processes(tmp_path):
    from equity_monitor.app import open_app
    from equity_monitor.util import Clock
    open_app(tmp_path, clock=Clock(AS_OF), policy_path=None, settings_path=None)      # migrate once
    code = f"""
import sys
from datetime import datetime, timezone
from equity_monitor.app import open_app
from equity_monitor.llm.budget import BudgetExceeded, reserve
from equity_monitor.util import Clock
app = open_app({str(tmp_path)!r}, clock=Clock(datetime(2026, 9, 30, 22, tzinfo=timezone.utc)), policy_path=None,
               settings_path=None)
app.settings = app.settings.model_copy(update={{"llm": app.settings.llm.model_copy(update={{"max_subscription_calls_per_day": 3}})}})
try:
    reserve(app, "claude_code", "m", None); print("OK")
except BudgetExceeded:
    print("REFUSED")
"""
    procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) for _ in range(8)]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    assert outs.count("OK") == 3 and outs.count("REFUSED") == 5                 # never more than the cap
    app = open_app(tmp_path, clock=Clock(AS_OF), policy_path=None, settings_path=None)
    assert app.conn.execute("SELECT COUNT(*) FROM llm_budget_entry WHERE provider='claude_code'").fetchone()[0] == 3


@pytest.mark.skipif(shutil.which("claude") is None, reason="Claude Code CLI not installed")
def test_real_cli_does_not_run_user_or_project_hooks_offline():
    """Harmless integration test on the installed CLI: local fake API, dummy key, temporary HOME with user hooks, a
    user MCP server and CLAUDE.md files. The control run must fire them; the isolated run must not."""
    from equity_monitor.llm.claude_code_check import isolation_test
    r = isolation_test("claude")
    assert r["control_hooks_fired"], "control run did not fire hooks: the test would prove nothing"
    assert r["control_claude_md_sent"]
    assert r["problems"] == [] and r["isolated_hooks_fired"] == []
    assert r["isolated_tools_offered"] == ["StructuredOutput"] and r["structured_output"] == {"ok": "isolated"}
