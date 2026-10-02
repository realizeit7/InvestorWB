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


# ------------------------------------------------------------------ stage 2: LLM judgment (no API, credential-free)
import os
import stat
import subprocess
import sys

from equity_monitor.llm.base import FixtureLLM, LLMRequest
from equity_monitor.llm.claude_code_provider import ClaudeCodeProvider


def _run(app):
    app.clock.set(AS_OF)
    build_demo(app)
    nasdaq, sec_map = _universe_inputs()
    years = [AS_OF.year - k for k in range(1, 6)]
    return run_finder(app, nasdaq_rows=nasdaq, sec_map=sec_map, frames=_frames([99000000 + i for i in range(6)], years),
                      deep_fetch=lambda u: find_security(app.conn, u.symbol), as_of=AS_OF)


def _fake_claude(tmp_path, payload: dict, exit_code=0):
    """A stand-in `claude` executable: records argv, cwd, stdin and environment, prints a result object."""
    log = tmp_path / "claude_call.json"
    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "cwd_files": os.listdir("."), "stdin": sys.stdin.read(),
           "has_api_key": "ANTHROPIC_API_KEY" in os.environ}}, open({str(log)!r}, "w"))
print(json.dumps({payload!r}))
sys.exit({exit_code})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def test_claude_code_provider_is_isolated_and_never_uses_an_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-stripped")
    out = {"type": "result", "subtype": "success", "is_error": False, "result": "",
           "structured_output": {"ok": True}, "usage": {"input_tokens": 1200, "output_tokens": 300},
           "total_cost_usd": 0.05, "modelUsage": {"claude-opus-5-5": {}}}
    script, log = _fake_claude(tmp_path, out)
    p = ClaudeCodeProvider(str(script), model="claude-opus-5-5", timeout_s=30)
    resp = p.complete(LLMRequest("t", "v", "SYSTEM PROMPT", "untrusted filing text", {"type": "object"}))
    call = json.loads(log.read_text())
    assert resp.status == "OK" and json.loads(resp.text) == {"ok": True} and resp.input_tokens == 1200
    assert call["has_api_key"] is False                                   # uses the logged-in account, not API billing
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"] and "--bare" not in argv
    assert argv[argv.index("--tools") + 1] == ""                          # no tools: filing text cannot make it act
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM PROMPT" and "--json-schema" in argv
    assert call["stdin"] == "untrusted filing text" and call["cwd_files"] == [] and "eqm_cc_" in call["cwd"]


def test_claude_code_provider_errors_are_explicit(tmp_path):
    script, _ = _fake_claude(tmp_path, {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "x"})
    r = ClaudeCodeProvider(str(script)).complete(LLMRequest("t", "v", "s", "u", {}))
    assert r.status == "ERROR" and "error_max_turns" in r.error
    bad, _ = _fake_claude(tmp_path, {"type": "result"}, exit_code=3)
    assert "exit 3" in ClaudeCodeProvider(str(bad)).complete(LLMRequest("t", "v", "s", "u", {})).error
    assert "not found" in ClaudeCodeProvider(str(tmp_path / "nope")).complete(LLMRequest("t", "v", "s", "u", {})).error


def _judgment(sym, verdict="RESEARCH_FURTHER", claims=()):
    return {"symbol": sym, "verdict": verdict, "research_priority": 4, "underrated_case": "margins expanding",
            "value_trap_risks": "customer concentration", "what_would_change_view": "margin reversal",
            "claims": list(claims)}


def test_judgments_annotate_but_never_reorder_or_extend_the_shortlist(app):
    from equity_monitor.research.finder_judge import judge_run, latest_judgments
    rid = _run(app)
    before = [(c["symbol"], c["rank"]) for c in shortlist(app, rid)]

    def responder(req):
        sym = json.loads(req.user.split("\n", 1)[1].split("\n\nRecent filing")[0])["symbol"]
        if sym == "ZZEXT":
            return _judgment(sym, "LIKELY_VALUE_TRAP")
        if sym == "ZZREV":
            return _judgment("AAPL")                                       # tries to judge another company
        return _judgment(sym)
    out = judge_run(app, FixtureLLM(responder), rid, fetch_text=False)
    js = latest_judgments(app, rid)
    assert js["ZZEXT"]["verdict"] == "LIKELY_VALUE_TRAP" and "ZZREV" not in js and "AAPL" not in js
    assert any(f["symbol"] == "ZZREV" for f in out["failed"])
    assert [(c["symbol"], c["rank"]) for c in shortlist(app, rid)] == before          # deterministic order unchanged
    assert app.conn.execute("SELECT COUNT(*) FROM recommendation").fetchone()[0] == 0


def test_subscription_calls_are_capped_per_day(app):
    from equity_monitor.research.finder_judge import judge_run
    rid = _run(app)
    s = app.settings.llm.model_copy(update={"max_subscription_calls_per_day": 2})
    app.settings = app.settings.model_copy(update={"llm": s})

    class FakeCC(FixtureLLM):
        name = "claude_code"
    p = FakeCC(lambda req: _judgment(json.loads(req.user.split("\n", 1)[1].split("\n\nRecent filing")[0])["symbol"]))
    out = judge_run(app, p, rid, fetch_text=False)
    assert len(out["judged"]) == 2 and "max_subscription_calls_per_day" in out["failed"][0]["error"]
    cost = app.conn.execute("SELECT amount_usd, units_json FROM cost_record WHERE provider='claude_code' LIMIT 1").fetchone()
    assert cost[0] == "0" and "subscription" in cost[1]


def test_interactive_pack_and_import_verify_claims(app, tmp_path):
    from equity_monitor.db.core import insert
    from equity_monitor.data.sec import store_passages
    from equity_monitor.research.finder_judge import export_pack, import_judgments, latest_judgments
    rid = _run(app)
    iss = app.conn.execute("SELECT issuer_id FROM security WHERE symbol='ZZADD'").fetchone()[0]
    insert(app.conn, "source_document", {"id": "doc_f", "provider": "FIXTURE", "doc_type": "10-K", "issuer_id": iss,
           "accession_no": "doc_f", "source_url": None, "title": "t", "fiscal_period_end": "2026-06-30",
           "filed_date": "2026-08-15", "public_at": "2026-08-15T20:05:00.000000Z", "public_at_basis": "PROVIDED",
           "retrieved_at": "2026-08-15T20:05:00.000000Z", "raw_object_id": None, "content_hash": None,
           "parser_version": None, "trust": "FIXTURE", "items": None, "limitations": None})
    src = "Revenue was $1.0 billion in fiscal 2026."
    store_passages(app, "doc_f", src)
    files = export_pack(app, rid, tmp_path / "pack", fetch_text=False)
    text = files["pack"].read_text()
    assert "doc_f#p0" in text and "ZZADD" in text and "import-judgments" in text
    data = {"run_id": rid, "judgments": [
        _judgment("ZZADD", claims=[{"text": "Revenue was $1.0 billion in fiscal 2026.", "claim_type": "FACT",
                                    "citations": [{"passage_id": "doc_f#p0", "quote": src}]},
                                   {"text": "Revenue was $9 billion in fiscal 2026.", "claim_type": "FACT",
                                    "citations": [{"passage_id": "doc_f#p0", "quote": src}]}]),
        _judgment("MSFT")]}
    files["template"].write_text(json.dumps(data))
    res = import_judgments(app, files["template"])
    assert res["stored"] == ["ZZADD"] and res["rejected"][0]["symbol"] == "MSFT"
    ver = latest_judgments(app, rid)["ZZADD"]["verification"]
    assert [v["status"] for v in ver] == ["VERIFIED", "FAILED"]
    assert latest_judgments(app, rid)["ZZADD"]["provider"] == "interactive"
