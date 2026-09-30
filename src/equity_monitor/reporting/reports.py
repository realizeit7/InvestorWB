"""Report builders (Markdown), writers (dated .md + .html) and machine-readable exports.

Every report starts with a banner saying whether content is ACTUAL, PAPER, FIXTURE, ILLUSTRATIVE or
HYPOTHETICAL and whether it is a PREVIEW. Proposed trades are never displayed as completed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..app import App
from ..data import calendar as cal
from ..data.rawstore import open_quality_issues
from ..db.core import all_rows, one
from ..decisions import recommend as rec_mod
from ..decisions.recommend import is_preview
from ..ledger.store import portfolio_kind
from ..ledger.views import portfolio_view
from ..research.thesis import history, original_version, current_version
from ..util import fmt_money, fmt_pct, from_json, iso_utc, to_json
from ..valuation.store import latest_valuation
from .markdown import md_to_html


def banner(app: App, portfolio_id: str) -> str:
    kind = portfolio_kind(app, portfolio_id)
    lines = []
    if kind == "FIXTURE":
        lines.append("> **FIXTURE** — synthetic demonstration data. Not market evidence; companies are fictional.")
    elif kind == "PAPER":
        lines.append("> **PAPER** — simulated portfolio; no real trades.")
    elif kind == "HYPOTHETICAL":
        lines.append("> **HYPOTHETICAL** — scenario only.")
    else:
        lines.append("> **ACTUAL** portfolio (from your recorded transactions).")
    if is_preview(app, portfolio_id):
        why = []
        if app.policy.status == "PREVIEW":
            why.append("policy thresholds are provisional and unapproved")
        if not app.settings.risk.confirmed:
            why.append("risk settings not confirmed")
        if kind != "ACTUAL":
            why.append(f"{kind.lower()} portfolio")
        lines.append(f"> **PREVIEW** — {', '.join(why)}. Not personalized advice.")
    lines.append("> Decision support only: nothing here places orders. Proposed trades are proposals, not executions.")
    return "\n".join(lines)


def _num(x, pct=False, money=False):
    if x is None:
        return "n/a"
    x = Decimal(str(x))
    if pct:
        return fmt_pct(x)
    if money:
        return fmt_money(x)
    return f"{x:,.2f}"


def portfolio_review_md(app: App, portfolio_id: str, as_of: datetime | None = None) -> str:
    as_of = as_of or app.now()
    view = portfolio_view(app, portfolio_id, cal.latest_completed_session(as_of))
    name = one(app.conn, "SELECT name FROM portfolio WHERE id=?", (portfolio_id,))["name"]
    md = [f"# Portfolio review — {name} — {view.as_of}", "", banner(app, portfolio_id), ""]
    md += ["## Summary", "",
           f"- NAV: {_num(view.nav, money=True)}" + ("" if view.nav is not None else f" (missing prices: {', '.join(view.missing_prices)})"),
           f"- Cash: {_num(view.cash, money=True)} (available {_num(view.available_cash, money=True)}, unsettled {_num(view.unsettled_cash, money=True)})",
           f"- Realized gain: {_num(view.realized_gain, money=True) if view.realized_gain is not None else 'unknown (' + str(view.realized_unknown_lots) + ' lot(s) without cost basis)'}",
           f"- Dividends: {_num(view.dividends, money=True)} · Fees: {_num(view.fees, money=True)}",
           f"- Open reconciliation issues: {len(view.open_issues)}", ""]
    md += ["## Holdings and current recommendation", "",
           "| Symbol | Shares | Price (date) | Value | Weight | Cost basis | Unrealized | Business | Action | MoS | Freshness |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for h in view.holdings:
        r = rec_mod.latest_for(app, portfolio_id, h.security_id) if h.security_type not in ("ETF", "FUND") else None
        act = r["action"] if r else ("ETF (tracked, no company valuation)" if h.security_type in ("ETF", "FUND") else "not reviewed")
        bus = r["business_assessment"] if r else "—"
        mos = _num(r["payload"].get("margin_of_safety"), pct=True) if r else "—"
        fresh = f"price {h.price_stale_sessions} session(s) old" if h.price_stale_sessions is not None else "no price"
        md.append(f"| {h.symbol} | {h.shares:,.4f} | {_num(h.price)} ({h.price_date or '—'}) | {_num(h.market_value, money=True)} | "
                  f"{_num(h.weight, pct=True)} | {_num(h.cost_basis, money=True) if h.cost_basis is not None else 'unknown'} | "
                  f"{_num(h.unrealized_gain, money=True) if h.unrealized_gain is not None else 'unknown'} | {bus} | **{act}** | {mos} | {fresh} |")
    wl = all_rows(app.conn, "SELECT w.security_id, s.symbol, w.status FROM watchlist_entry w JOIN security s ON s.id=w.security_id "
                            "WHERE w.status IN ('APPROVED','RESEARCH') ORDER BY s.symbol")
    if wl:
        md += ["", "## Watchlist", "", "| Symbol | Status | Action | MoS | Explanation |", "|---|---|---|---|---|"]
        for w in wl:
            r = rec_mod.latest_for(app, portfolio_id, w["security_id"])
            md.append(f"| {w['symbol']} | {w['status']} | {r['action'] if r else '—'} | "
                      f"{_num(r['payload'].get('margin_of_safety'), pct=True) if r else '—'} | {r['explanation'][:120] if r else ''} |")
    md += ["", "## Recommendation details", ""]
    for h in [x for x in view.holdings if x.security_type not in ("ETF", "FUND")] + [
            type("W", (), {"security_id": w["security_id"], "symbol": w["symbol"]}) for w in wl]:
        r = rec_mod.latest_for(app, portfolio_id, h.security_id)
        if not r:
            continue
        p = r["payload"]
        md += [f"### {h.symbol}: {r['action']} (business {r['business_assessment']})", "",
               f"- As of {r['as_of']} · previous action: {r['previous_action'] or 'none'} · reasons: {', '.join(r['reason_codes'])}",
               f"- {r['explanation']}",
               f"- Price {_num(p.get('price'))} vs values bear/base/bull: {_num(p['downside'].get('bear_value'))} / "
               f"{_num(p['downside'].get('base_value'))} / {_num(p['downside'].get('bull_value'))} (scenarios, not forecasts)",
               f"- Bear-case downside from price: {_num(p['downside'].get('bear_downside'), pct=True)}",
               f"- Concentration: issuer {_num(p['concentration'].get('issuer_weight'), pct=True)} (limit {_num(p['concentration'].get('issuer_limit'), pct=True)}), "
               f"sector {p['concentration'].get('sector')} {_num(p['concentration'].get('sector_weight'), pct=True)} (limit {_num(p['concentration'].get('sector_limit'), pct=True)})",
               f"- Freshness: price date {p['freshness'].get('price_date')}, filings checked {_num(p['freshness'].get('filings_checked_hours_ago'))} h ago, "
               f"latest period {p['freshness'].get('latest_period_end')}",
               f"- Next review: {p.get('next_review')}"]
        if p.get("missing"):
            md.append("- Missing / to resolve: " + "; ".join(p["missing"]))
        if p.get("urgent"):
            md.append("- **Urgent:** " + "; ".join(p["urgent"]))
        if p.get("proposed_trade"):
            t = p["proposed_trade"]
            md.append(f"- Proposed (not executed): {t.get('side')} about {_num(t.get('amount'), money=True)}"
                      f"{' — ' + t['note'] if t.get('note') else ''}")
        md.append("- What would change this: " + "; ".join(p.get("change_conditions") or []))
        ch = p.get("changes", {})
        if not ch.get("first_review"):
            md.append(f"- Since {ch.get('since')}: action {ch.get('previous_action')} → {r['action']}; "
                      f"{len(ch.get('new_documents', []))} new filing(s); price {_num(ch['price'].get('before'))} → {_num(ch['price'].get('now'))}")
        md.append("")
    if view.open_issues:
        md += ["## Reconciliation issues", "", "| Type | Severity | Security | Detail |", "|---|---|---|---|"]
        for i in view.open_issues:
            md.append(f"| {i['issue_type']} | {i['severity']} | {i['security_id'] or ''} | {i['detail_json'][:140]} |")
    return "\n".join(md) + "\n"


def weekly_digest_md(app: App, portfolio_id: str, as_of: datetime | None = None) -> str:
    as_of = as_of or app.now()
    since = iso_utc(as_of - timedelta(days=7))
    view = portfolio_view(app, portfolio_id, cal.latest_completed_session(as_of))
    md = [f"# Weekly digest — week ending {as_of.date()}", "", banner(app, portfolio_id), "", "## Current action per holding", "",
          "| Symbol | Action | Business | Changed this week | Explanation |", "|---|---|---|---|---|"]
    actionable = False
    for h in view.holdings:
        if h.security_type in ("ETF", "FUND"):
            md.append(f"| {h.symbol} | ETF — tracked only | — | — | passive sleeve; no company valuation |")
            continue
        r = rec_mod.latest_for(app, portfolio_id, h.security_id)
        if r is None:
            md.append(f"| {h.symbol} | not reviewed | — | — | run `eqm review` |")
            actionable = True
            continue
        changed = r["as_of"] >= since and r["previous_action"] not in (None, r["action"])
        actionable |= r["action"] != "HOLD"
        md.append(f"| {h.symbol} | **{r['action']}** | {r['business_assessment']} | {'yes (was ' + r['previous_action'] + ')' if changed else 'no'} | {r['explanation'][:110]} |")
    ev = all_rows(app.conn, "SELECT e.*, s.symbol FROM detected_event e LEFT JOIN security s ON s.id=e.security_id "
                            "WHERE e.detected_at>=? AND e.severity IN ('CRITICAL','MATERIAL') ORDER BY e.detected_at", (since,))
    md += ["", "## Material evidence this week", ""]
    md += [f"- {e['severity']}: {e['symbol'] or ''} {e['event_type']} — {from_json(e['payload_json']).get('label', '')} (public {e['public_at']})"
           for e in ev] or ["- None detected by scheduled checks (polling, not real-time)."]
    md += ["", "## Upcoming review events (next 30 days)", ""]
    upcoming = []
    for h in view.holdings:
        r = rec_mod.latest_for(app, portfolio_id, h.security_id)
        if r and r["payload"].get("next_review") and r["payload"]["next_review"] <= (as_of.date() + timedelta(days=30)).isoformat():
            upcoming.append(f"- {h.symbol}: {r['payload']['next_review']}")
    md += upcoming or ["- None scheduled."]
    dq = open_quality_issues(app)
    md += ["", "## Unresolved data issues", ""]
    md += [f"- {i['severity']} {i['issue_code']}: {i['detail'][:140]}" for i in dq] or ["- None open."]
    if view.open_issues:
        actionable = True
        md += [f"- Reconciliation: {i['issue_type']} ({i['severity']})" for i in view.open_issues]
    md += ["", "## Bottom line", "",
           "No action indicated this week." if not actionable and not dq else
           "Action or review indicated — see the rows marked ADD, TRIM, EXIT or REVIEW and the issues above."]
    return "\n".join(md) + "\n"


def allocation_md(app: App, proposal: dict) -> str:
    p = proposal["payload"] if "payload" in proposal else proposal
    md = [f"# Monthly allocation proposal — {p['as_of'][:10]}", "", banner(app, p["portfolio_id"]), "",
          f"- Contribution basis: **{p['kind']}**" + (" (not real cash until recorded as a DEPOSIT)" if p["kind"] == "HYPOTHETICAL" else ""),
          f"- Budget: {_num(p['budget'], money=True)} (deployable settled cash {_num(p['deployable_cash'], money=True)})",
          f"- NAV before/after contribution: {_num(p['nav_before'], money=True)} / {_num(p['nav_after'], money=True)}", "",
          "| Rank | Symbol | MoS | Current weight | Proposed $ | Est. shares | Fee | Proposed weight | Binding constraint |",
          "|---|---|---|---|---|---|---|---|---|"]
    for l in p["lines"]:
        md.append(f"| {l['rank']} | {l['symbol']} | {_num(l['margin_of_safety'], pct=True)} | {_num(l['current_weight'], pct=True)} | "
                  f"{_num(l['amount'], money=True)} | {_num(l['shares'])} | {_num(l['fee'], money=True)} | {_num(l['proposed_weight'], pct=True)} | "
                  f"{l['binding'] or ''} {l['note']} |")
    md += ["", f"**Remaining unallocated cash: {_num(p['remaining_cash'], money=True)}**", ""]
    if p["excluded"]:
        md += ["Excluded candidates:", ""] + [f"- {e['symbol']}: {e['reason']}" for e in p["excluded"]]
    if p["notes"]:
        md += ["", "Notes:", ""] + [f"- {n}" for n in p["notes"]]
    md += ["", "Record your decision with `eqm decide --allocation <id> ACCEPT|REJECT|OVERRIDE`. "
               "After trading, record the actual fills with `eqm ledger add` or a CSV import."]
    return "\n".join(md) + "\n"


def company_md(app: App, portfolio_id: str, security_id: str) -> str:
    s = one(app.conn, "SELECT s.symbol, i.name, i.cik, i.sector FROM security s LEFT JOIN issuer i ON i.id=s.issuer_id WHERE s.id=?",
            (security_id,))
    md = [f"# {s['symbol']} — {s['name']}", "", banner(app, portfolio_id), "", f"CIK {s['cik']} · sector {s['sector']}", ""]
    orig, cur = original_version(app, security_id), current_version(app, security_id)
    for title, v in (("Original approved thesis", orig), ("Current approved thesis", cur)):
        md += [f"## {title}", ""]
        if v is None:
            md.append("- none")
            continue
        c = v.content
        md += [f"- Version {v.version_no} ({v.author}, approved {v.approved_at}); reason: {v.change_reason}",
               f"- How it makes money: {c['business_model']}", f"- What the price requires: {c['valuation_requires']}",
               f"- Where we differ: {c['our_view_differs']}", f"- Strongest counterargument: {c['counterargument']}",
               f"- Value realization: {c['value_realization']}", f"- Key risks: {', '.join(c['key_risks'])}",
               f"- Assumptions (not facts): {', '.join(c['assumptions'])}", f"- Next review: {c['next_review_date']}",
               "- Invalidation conditions: " + "; ".join(x["description"] for x in v.conditions),
               "- Milestones: " + "; ".join(x["description"] for x in v.milestones)]
        for cl in v.claims:
            md.append(f"  - [{cl['claim_type']}/{cl['verification']}] {cl['text']}")
        md.append("")
    md += ["## Thesis history", "", "| Version | Author | Created | Approved | Reason |", "|---|---|---|---|---|"]
    md += [f"| {v.version_no} | {v.author} | {v.created_at[:10]} | {v.approved_at[:10] if v.approved_at else 'draft'} | {v.change_reason} |"
           for v in history(app, security_id)]
    md += ["", "## Valuation versions (per share)", "", "| Version | Created | Approved | Bear | Base | Bull | Terminal share |", "|---|---|---|---|---|---|---|"]
    for r in all_rows(app.conn, "SELECT v.*, a.approved_at FROM valuation_version v LEFT JOIN valuation_approval a ON "
                                "a.valuation_version_id=v.id WHERE v.security_id=? ORDER BY version_no", (security_id,)):
        out = from_json(r["outputs_json"])
        md.append(f"| {r['version_no']} | {r['created_at'][:10]} | {r['approved_at'][:10] if r['approved_at'] else 'no'} | "
                  f"{_num(r['bear_value_ps'])} | {_num(r['base_value_ps'])} | {_num(r['bull_value_ps'])} | {_num(out['base']['terminal_share'], pct=True)} |")
    md += ["", "## Recommendation history", "", "| As of | Action | Business | Price | MoS | Reasons | Owner decision |", "|---|---|---|---|---|---|---|"]
    for r in rec_mod.history_for(app, portfolio_id, security_id):
        dec = rec_mod.decisions_for(app, r["id"])
        dtxt = "; ".join(f"{d['decision']}{'→' + d['override_action'] if d['override_action'] else ''}" for d in dec)
        md.append(f"| {r['as_of'][:16]} | {r['action']} | {r['business_assessment']} | {_num(r['payload'].get('price'))} | "
                  f"{_num(r['payload'].get('margin_of_safety'), pct=True)} | {', '.join(r['reason_codes'])} | {dtxt} |")
    return "\n".join(md) + "\n"


def health_md(app: App) -> str:
    from ..monitoring.health import system_health
    h = system_health(app)
    md = [f"# System health — {app.now_iso()}", "", f"**Overall: {h['overall']}**", ""]
    md += [f"- {w}" for w in h["warnings"]] or ["- No warnings."]
    md += ["", "## Jobs", "", "| Job | Last run | Status | Last success | Next run |", "|---|---|---|---|---|"]
    for j in h["jobs"]:
        lr = j["last_run"] or {}
        md.append(f"| {j['job']} | {lr.get('started_at', 'never')} | {lr.get('status', '—')} | {j['last_success'] or 'never'} | {j['next_run']} |")
    md += ["", "## Providers", "", "| Provider | Check | Last success | Last failure | Recent failures |", "|---|---|---|---|---|"]
    for p in h["providers"]:
        md.append(f"| {p['provider']} | {p['check_type']} | {p['last_success'] or 'never'} | {p['last_failure'] or '—'} | {p['recent_failures']} |")
    md += ["", "## Costs this month", "", f"- LLM: {_num(h['costs']['llm_month'], money=True)} (unknown-cost calls: {h['costs']['llm_unknown']})",
           f"- Data: {_num(h['costs']['data_month'], money=True)}", "", "## Deliveries", ""]
    md += [f"- {k}: {v}" for k, v in h["outbox"].items()] or ["- External delivery disabled (local inbox only)."]
    return "\n".join(md) + "\n"


def export_decisions(app: App, portfolio_id: str) -> dict:
    recs = [rec_mod.get(app, r["id"]) for r in all_rows(app.conn, "SELECT id FROM recommendation WHERE portfolio_id=? ORDER BY as_of, rowid",
                                                         (portfolio_id,))]
    return {"portfolio_id": portfolio_id, "kind": portfolio_kind(app, portfolio_id), "exported_at": app.now_iso(),
            "policy_versions": [dict(r) for r in all_rows(app.conn, "SELECT id, name, content_hash, status FROM policy_version")],
            "recommendations": recs,
            "decisions": [dict(r) for r in all_rows(app.conn, "SELECT * FROM user_decision ORDER BY decided_at")],
            "allocations": [dict(r) for r in all_rows(app.conn, "SELECT id, as_of, contribution_kind, budget, remaining_cash, is_preview, label "
                                                                "FROM allocation_proposal WHERE portfolio_id=?", (portfolio_id,))]}


def write_report(app: App, name: str, md: str, day: date | None = None, out_dir: Path | None = None) -> tuple[Path, Path]:
    day = day or app.now().date()
    d = (out_dir or app.reports_dir) / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    mdp, htp = d / f"{name}.md", d / f"{name}.html"
    mdp.write_text(md, encoding="utf-8")
    htp.write_text(md_to_html(md, name), encoding="utf-8")
    return mdp, htp


def write_json(app: App, name: str, data: dict, day: date | None = None, out_dir: Path | None = None) -> Path:
    day = day or app.now().date()
    d = (out_dir or app.reports_dir) / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(json.loads(to_json(data)), indent=1), encoding="utf-8")
    return p
