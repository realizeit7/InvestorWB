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
from ..market.lookthrough import portfolio_market_exposure
from ..market.snapshot import snapshot_as_of
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
           "| Symbol | Shares | Price (date) | Value | Weight | Cost basis | Unrealized | Business | Action | Purchases | MoS | Freshness |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for h in view.holdings:
        r = rec_mod.latest_for(app, portfolio_id, h.security_id) if h.security_type not in ("ETF", "FUND") else None
        act = r["action"] if r else ("ETF (tracked, no company valuation)" if h.security_type in ("ETF", "FUND") else "not reviewed")
        bus = r["business_assessment"] if r else "—"
        mos = _num(r["payload"].get("margin_of_safety"), pct=True) if r else "—"
        fresh = f"price {h.price_stale_sessions} session(s) old" if h.price_stale_sessions is not None else "no price"
        md.append(f"| {h.symbol} | {h.shares:,.4f} | {_num(h.price)} ({h.price_date or '—'}) | {_num(h.market_value, money=True)} | "
                  f"{_num(h.weight, pct=True)} | {_num(h.cost_basis, money=True) if h.cost_basis is not None else 'unknown'} | "
                  f"{_num(h.unrealized_gain, money=True) if h.unrealized_gain is not None else 'unknown'} | {bus} | **{act}** | "
                  f"{(r.get('purchase_eligibility') or 'UNKNOWN') if r else '—'} | {mos} | {fresh} |")
    wl = all_rows(app.conn, "SELECT w.security_id, s.symbol, w.status FROM watchlist_entry w JOIN security s ON s.id=w.security_id "
                            "WHERE w.status IN ('APPROVED','RESEARCH') ORDER BY s.symbol")
    if wl:
        md += ["", "## Watchlist", "", "| Symbol | Status | Action | Purchases | MoS | Explanation |", "|---|---|---|---|---|---|"]
        for w in wl:
            r = rec_mod.latest_for(app, portfolio_id, w["security_id"])
            md.append(f"| {w['symbol']} | {w['status']} | {r['action'] if r else '—'} | {(r.get('purchase_eligibility') or 'UNKNOWN') if r else '—'} | "
                      f"{_num(r['payload'].get('margin_of_safety'), pct=True) if r else '—'} | {r['explanation'][:120] if r else ''} |")
    snap = snapshot_as_of(app, as_of)
    md += ["", "## Market context used by these reviews", ""]
    if snap:
        md += [f"- Shared snapshot `{snap['id']}` as of {snap['as_of']} (session {snap['session']}); full detail: `eqm market show`",
               "- Flags (conditions, not forecasts): " + (", ".join(sorted({f['flag'] for f in snap['flags']})) or "none"),
               "- Missing inputs (UNKNOWN, not neutral): " + (", ".join(snap["missing"]) or "none"),
               "- Not available: " + "; ".join(snap["unavailable"])]
        try:
            lt = portfolio_market_exposure(app, portfolio_id, view.as_of)
            md.append(f"- Portfolio market exposure counted once: beta to SPY {_num(lt['portfolio_beta_spy'])}, to QQQ "
                      f"{_num(lt['portfolio_beta_qqq'])} (cash weight {_num(lt['cash_weight'], pct=True)}; beta unknown for "
                      f"{', '.join(lt['beta_unknown_for']) or 'none'}). Company reviews add no separate market-move penalty.")
        except Exception as exc:  # noqa: BLE001 - reporting must not hide the rest of the review
            md.append(f"- Portfolio market exposure: unavailable ({exc})")
    else:
        md.append("- **No market snapshot**: broad-market, sector and economic conditions are UNKNOWN for these reviews.")
    md += ["", "## Per-holding review", ""]
    for h in [x for x in view.holdings if x.security_type not in ("ETF", "FUND")] + [
            type("W", (), {"security_id": w["security_id"], "symbol": w["symbol"]}) for w in wl]:
        r = rec_mod.latest_for(app, portfolio_id, h.security_id)
        if not r:
            continue
        md += holding_section_md(r) + [""]
    if view.open_issues:
        md += ["## Reconciliation issues", "", "| Type | Severity | Security | Detail |", "|---|---|---|---|"]
        for i in view.open_issues:
            md.append(f"| {i['issue_type']} | {i['severity']} | {i['security_id'] or ''} | {i['detail_json'][:140]} |")
    return "\n".join(md) + "\n"


def holding_section_md(r: dict) -> list[str]:
    """The ten required items for one holding (works for older recommendations without market context)."""
    p = r["payload"]
    cc = p.get("current_conditions") or {}
    lt = p.get("long_term_case") or {}
    chains = p.get("chains") or []
    ctx = (p.get("market_context") or {})
    md = [f"### {p['symbol']}: {r['action']} · purchases {r.get('purchase_eligibility') or 'UNKNOWN'}", ""]
    md.append(f"1. **Action and purchase eligibility** — long-term action **{r['action']}** (business {r['business_assessment']}); "
              f"purchases **{r.get('purchase_eligibility') or 'UNKNOWN'}** (fundamental-only baseline: {r.get('baseline_eligibility') or 'n/a'}). "
              f"{r['explanation']}")
    for b in cc.get("blocks", []):
        md.append(f"   - blocked: {b}")
    for pz in cc.get("pauses", []):
        md.append(f"   - paused: {pz['code']} — {pz['detail']} · reassess when {pz['reassess_condition']}"
                  + (f" (by {pz['reassess_on']})" if pz.get("reassess_on") else ""))
    o, c = lt.get("original_thesis"), lt.get("current_thesis")
    md.append("2. **Thesis** — " + (f"original v{o['version_no']} (approved {str(o['approved_at'])[:10]}); current v{c['version_no']}"
                                     f"{' — changed: ' + c['change_reason'] if lt.get('thesis_changed_since_original') else ' (unchanged)'}; "
                                     f"status {lt.get('business_assessment')}" if o and c else "no approved thesis"))
    def level(lv, title, n):
        rel = [ch for ch in chains if ch["level"] == lv]
        lines = [f"{n}. **{title}** — " + ("; ".join((ctx.get("developments") or {}).get(lv.lower(), [])[:6]) or "none recorded")]
        for ch in rel:
            lines.append(f"   - `{ch['cluster_key']}` → relevance {ch['relevance'].get('status')}"
                         f"{' via ' + ', '.join(ch['relevance'].get('exposures') or []) if ch['relevance'].get('exposures') else ''}"
                         f" → mechanism: {ch['mechanism']} → implication: {ch['implication'].get('type')} → **{ch['effect']}** ({ch['reason']})")
        return lines
    md += level("MARKET", "Broad-market developments", 3)
    md += level("SECTOR", "Sector / industry developments", 4)
    md += level("COMPANY", "Company-specific developments", 5)
    sup = [e for e in p.get("evidence", []) if e.get("supports", True)]
    con = [e for e in p.get("evidence", []) if not e.get("supports", True)]
    md.append(f"6. **Evidence** — supporting: " + ("; ".join(f"[{e['verification']}] {e['text']}" for e in sup) or "none cited")
              + " · contradicting: " + ("; ".join(f"[{e['verification']}] {e['text']}" for e in con) or "none cited")
              + (" · research tasks: " + "; ".join(f"{x['reason']} ({x['cluster']})" for x in cc.get("research", [])) if cc.get("research") else ""))
    vc = p.get("valuation_changes") or {}
    props = vc.get("proposals") or []
    md.append("7. **Valuation assumptions** — " + ("; ".join(f"proposed {x['assumption']} {_num(x['current'], pct=True)} → "
                                                          f"{_num(x['proposed'], pct=True)} ({x['status']})" for x in props)
                                                 or "no changes proposed")
              + (f"; stress: bear {_num(vc['stress_downside']['bear_value'])} → {_num(vc['stress_downside']['stressed_bear_value'])} "
                 f"({vc['stress_downside']['shift']}, context only)" if vc.get("stress_downside") else ""))
    pi = p.get("position_implications") or {}
    md.append(f"8. **Position size and next contribution** — {pi.get('summary', 'n/a')}")
    fr = p.get("freshness", {})
    md.append(f"9. **Missing data and freshness** — price {fr.get('price_date')} ({fr.get('price_stale_sessions')} session(s) old); "
              f"filings checked {_num(fr.get('filings_checked_hours_ago'))} h before; latest period {fr.get('latest_period_end')}; "
              f"market snapshot {ctx.get('snapshot_as_of') or 'NONE'}; missing: " + ("; ".join(p.get("missing") or []) or "none"))
    changes = list(p.get("change_conditions") or []) + [f"pause lifts when {pz['reassess_condition']}" for pz in cc.get("pauses", [])]
    md.append("10. **What would change the decision** — " + ("; ".join(changes) or "n/a"))
    if p.get("urgent"):
        md.append("    - **Urgent:** " + "; ".join(p["urgent"]))
    ch = p.get("changes", {})
    if not ch.get("first_review"):
        el = ch.get("eligibility") or {}
        md.append(f"    - Since {ch.get('since')}: action {ch.get('previous_action')} → {r['action']}; purchases "
                  f"{el.get('before')} → {el.get('now')}; {len(ch.get('new_documents', []))} new filing(s)")
    md.append(f"    - Traceability: recommendation `{r['id']}`, snapshot `{r.get('market_snapshot_id')}`, exposure profile "
              f"`{r.get('exposure_version_id')}`, thesis `{r['thesis_version_id']}`, valuation `{r['valuation_version_id']}`, "
              f"policy `{r['policy_version_id']}`")
    return md


def market_context_md(app: App, snapshot: dict) -> str:
    md = [f"# Market context — {snapshot['session']}", "",
          f"Snapshot `{snapshot['id']}` as of {snapshot['as_of']}. Deterministic, point-in-time; referenced by company reviews.", "",
          "## Broad market (reference ETFs and indices)", "", "| Instrument | Status | 1m | 3m | 12m | From 52w high | Realized vol 3m |",
          "|---|---|---|---|---|---|---|"]
    for sym, m in snapshot["instruments"].items():
        md.append(f"| {sym} | {m.get('status')} | {_num(m.get('ret_1m'), pct=True)} | {_num(m.get('ret_3m'), pct=True)} | "
                  f"{_num(m.get('ret_12m'), pct=True)} | {_num(m.get('drawdown_from_52w_high'), pct=True)} | {_num(m.get('realized_vol_3m'), pct=True)} |")
    md += ["", f"VIX / VIX3M ratio: {_num(snapshot.get('implied_vol_term_ratio'))} (implied volatility term structure; not a probability).", "",
           "## Economy and financial markets", "", "| Series | Status | Value | Period | Public at | Δ1m | Δ3m | y/y | Vintage |",
           "|---|---|---|---|---|---|---|---|---|"]
    for k, v in snapshot["indicators"].items():
        md.append(f"| {v.get('name')} (`{k}`) | {v['status']} | {v.get('value', '—')} | {v.get('period', '—')} | {v.get('public_at', '—')} | "
                  f"{_num(v.get('chg_1m'))} | {_num(v.get('chg_3m'))} | {_num(v.get('pct_12m'), pct=True)} | {v.get('vintage_basis', '—')} |")
    md += ["", "## Sectors (SPDR sector ETFs)", "", "| Sector | ETF | Status | 1m | 3m | vs SPY 3m |", "|---|---|---|---|---|---|"]
    for sname, v in snapshot["sectors"].items():
        md.append(f"| {sname} | {v['etf']} | {v.get('status')} | {_num(v.get('ret_1m'), pct=True)} | {_num(v.get('ret_3m'), pct=True)} | "
                  f"{_num(v.get('relative_to_spy_3m'), pct=True)} |")
    md += ["", "## Flags (conditions, not forecasts)", ""]
    md += [f"- **{f['flag']}** ({f['factor'] or 'broad market'}): {f['observed']} — threshold {f['threshold']}" for f in snapshot["flags"]] or ["- none"]
    md += ["", "## Missing, stale and unavailable", "", "- Missing (UNKNOWN): " + (", ".join(snapshot["missing"]) or "none"),
           "- Stale: " + (", ".join(snapshot["stale"]) or "none"),
           "- Backfilled current-vintage history (may include later revisions): " + (", ".join(snapshot["revision_caveat"]) or "none"),
           "- Not available: " + "; ".join(snapshot["unavailable"])] + [f"- {n}" for n in snapshot["notes"]]
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
          f"- NAV before/after contribution: {_num(p['nav_before'], money=True)} / {_num(p['nav_after'], money=True)}"
          + (f" (after fees {_num(p.get('nav_after_fees'), money=True)})" if p.get("nav_after_fees") is not None else ""),
          "- Proposed weight = aggregate issuer weight (all share classes, current + proposed) after rounding and fees.", "",
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
    if p.get("validated"):
        md += ["", "Decisions re-validated at the cutoff (current policy and evidence):", ""] + [
            f"- {v['symbol']}: {v['action']} / purchases {v['purchase_eligibility']} — {v['recommendation_id']} "
            f"({'equivalent earlier review reused' if v['reused_equivalent'] else 'new review'} as of {v['recommendation_as_of']})"
            for v in p["validated"]]
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


def finder_md(app: App, run_id: str) -> str:
    """Company finder shortlist: research candidates only (never recommendations)."""
    import json as _json
    from ..research.finder import shortlist
    from ..research.finder_judge import latest_judgments
    run = one(app.conn, "SELECT * FROM finder_run WHERE id=?", (run_id,))
    src = _json.loads(run["sources_json"])
    judg = latest_judgments(app, run_id)
    cands = shortlist(app, run_id)

    def pct(v):
        return "—" if v is None else f"{float(v):.0%}"
    md = [f"# Company finder — research candidates ({run['session_date']})", "",
          "> **RESEARCH CANDIDATES, not recommendations.** A deterministic screen flagged these as possibly under-rated; "
          "nothing is bought or added to the watchlist automatically. DCF figures use ILLUSTRATIVE unapproved defaults. "
          "The finder has no demonstrated stock-selection edge; it is measured prospectively against SPY "
          "(`eqm finder evaluate`).", "",
          f"- Universe: {run['universe_count']} US companies (market cap ≥ ${app.policy.finder.min_market_cap_usd:,.0f}); "
          f"preliminary rank {run['prelim_ranked']}; deep dive {run['deep_count']}; shortlist {run['shortlist_count']}",
          f"- Excluded from the universe: " + ", ".join(f"{k} {v}" for k, v in sorted(src.get("universe_dropped", {}).items())),
          f"- Run `{run_id}`, data as of {run['as_of']}, policy `{run['policy_version_id']}`", "",
          "| # | Symbol | Sector | Score | Quality | Value | Growth priced in vs 3y actual | DCF MoS* | LLM view |",
          "|---|---|---|---|---|---|---|---|---|"]
    traps = []
    for c in cands:
        m, sc, j = c["metrics"], c["scores"], judg.get(c["symbol"])
        view = f"{j['verdict']} (priority {j['priority']}, {j['provider']})" if j else "not judged yet"
        if j and j["verdict"] == "LIKELY_VALUE_TRAP":
            traps.append(c["symbol"])
        gap = (f"{pct(m.get('implied_revenue_growth'))} vs {pct(m.get('revenue_cagr_3y'))}"
               if m.get("implied_revenue_growth") is not None else m.get("expectations_gap_note", "—"))
        md.append(f"| {c['rank']} | {c['symbol']} | {c.get('sector') or '—'} | {float(c['score']):.2f} | {pct(sc.get('quality'))} | "
                  f"{pct(sc.get('value'))} | {gap} | {pct(m.get('dcf_margin_of_safety'))} | {view} |")
    md += ["", "*Scores are percentiles within the deep-dive set; DCF margin of safety is shown, not weighted.", ""]
    if traps:
        md += ["**LLM flagged as likely value traps (still listed in deterministic order above):** " + ", ".join(traps), ""]
    for c in cands:
        j = judg.get(c["symbol"])
        if not j:
            continue
        ct = j["content"]
        md += [f"## {c['rank']}. {c['symbol']} — LLM opinion ({j['provider']}; not a fact, not a decision)", "",
               f"- Under-rated case: {ct['underrated_case']}", f"- Value-trap risks: {ct['value_trap_risks']}",
               f"- What would change the view: {ct['what_would_change_view']}"]
        for v in j["verification"]:
            md.append(f"  - [{v['type']}/{v['status']}] {v['text']}")
        md.append("")
    warns = _json.loads(run["warnings_json"])
    if warns:
        md += ["Warnings:", ""] + [f"- {w}" for w in warns[:20]] + [""]
    md += ["Next steps: research a name with `eqm finder promote SYMBOL` (adds it to the watchlist as RESEARCH), then the "
           "usual valuation → thesis → approval workflow. Judge the shortlist without the API: `eqm finder judge` "
           "(llm.provider claude_code) or `eqm finder pack` and ask Claude in a Claude Code session."]
    return "\n".join(md) + "\n"
