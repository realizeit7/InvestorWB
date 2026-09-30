"""Command-line interface: ``eqm <command>``. Run ``eqm --help`` or ``eqm <command> --help``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from .app import App, open_app
from .data import calendar as cal
from .util import fmt_money, fmt_pct, iso_utc, parse_date, to_json


def _app(args) -> App:
    return open_app(args.home, policy_path=args.policy, settings_path=args.settings)


def _pf(app: App, name: str | None) -> str:
    from .ledger.store import portfolio_by_name
    name = name or app.settings.active_portfolio
    if not name:
        raise SystemExit("specify --portfolio (or set active_portfolio in config/user.yaml)")
    return portfolio_by_name(app, name)["id"]


def _acct(app: App, name_or_id: str) -> str:
    r = app.conn.execute("SELECT id FROM account WHERE id=? OR name=?", (name_or_id, name_or_id)).fetchall()
    if len(r) != 1:
        raise SystemExit(f"account {name_or_id!r} not found or ambiguous; use the account id")
    return r[0]["id"]


def _sid(app: App, symbol: str) -> str:
    from .data.securities import resolve
    try:
        return resolve(app.conn, symbol)
    except KeyError as exc:
        raise SystemExit(str(exc))


def _print(obj) -> None:
    print(json.dumps(json.loads(to_json(obj)), indent=1))


# ------------------------------------------------------------------ commands
def cmd_init(args):
    app = _app(args)
    for src, dst in (("config/user.example.yaml", "config/user.yaml"), ("config/policy.example.yaml", "config/policy.yaml")):
        if Path(src).exists() and not Path(dst).exists():
            shutil.copy(src, dst)
            print(f"created {dst} from {src} — edit it (it is git-ignored)" if "user" in dst else f"created {dst}")
    print(f"database: {app.home / 'equity_monitor.sqlite'} (migrations applied)")


def cmd_portfolio(args):
    from .ledger.store import create_account, create_portfolio
    app = _app(args)
    if args.action == "create":
        print(create_portfolio(app, args.name, args.kind))
    elif args.action == "add-account":
        print(create_account(app, _pf(app, args.portfolio), args.name, broker=args.broker, tax_status=args.tax_status,
                             settlement_days=args.settlement_days))
    elif args.action == "list":
        for p in app.conn.execute("SELECT * FROM portfolio ORDER BY created_at"):
            accts = app.conn.execute("SELECT id, name, tax_status FROM account WHERE portfolio_id=?", (p["id"],)).fetchall()
            print(f"{p['name']} [{p['kind']}] {p['id']}: " + ", ".join(f"{a['name']} ({a['id']}, {a['tax_status']})" for a in accts))


def cmd_import(args):
    app = _app(args)
    acct = _acct(app, args.account)
    if args.kind == "transactions":
        from .ledger.csv_import import import_csv
        rep = import_csv(app, acct, args.file)
        print(rep.summary())
        for r in rep.rejected:
            print(f"  REJECTED row {r['row']}: {r['error']}")
    else:
        from .ledger.reconcile import import_snapshot, reconcile_snapshot
        sid = import_snapshot(app, acct, parse_date(args.as_of), args.file)
        found = reconcile_snapshot(app, sid)
        print(f"snapshot {sid}: {len(found)} discrepancy(ies) recorded (ledger unchanged)")
        for f in found:
            print("  ", f)


def cmd_ledger(args):
    from .ledger.store import NewEvent, load_events, record_events, reverse_event
    app = _app(args)
    if args.action == "add":
        acct = _acct(app, args.account)
        sid = None
        if args.symbol:
            from .data.securities import register_security
            sid = register_security(app.conn, app.now_iso(), args.symbol, security_type=args.security_type)
        ev = NewEvent(args.type.upper(), parse_date(args.date), sid, quantity=_d(args.quantity), price=_d(args.price),
                      fees=_d(args.fees), amount=_d(args.amount), cost_basis_total=_d(args.cost_basis),
                      ratio_num=_d(args.split_to), ratio_den=_d(args.split_from), external_id=args.external_id, note=args.note)
        res = record_events(app, acct, [ev], recorded_by="user:cli")
        print(f"inserted={res.inserted} duplicates={len(res.duplicates)} rejected={res.rejected}")
    elif args.action == "reverse":
        res = reverse_event(app, args.event_id, args.reason)
        print(f"reversal recorded: {res.inserted}")
    elif args.action == "list":
        for e in load_events(app, _acct(app, args.account)):
            sym = app.conn.execute("SELECT symbol FROM security WHERE id=?", (e.security_id,)).fetchone() if e.security_id else None
            print(f"{e.seq:>5} {e.trade_date} {e.event_type:<16} {sym['symbol'] if sym else '':<6} qty={e.quantity} "
                  f"px={e.price} amt={e.amount} fees={e.fees} {e.id}" + (f" reverses {e.reverses_event_id}" if e.reverses_event_id else ""))


def _d(x):
    from .util import D
    return D(x) if x is not None else None


def cmd_show(args):
    from .ledger.views import portfolio_view
    app = _app(args)
    v = portfolio_view(app, _pf(app, args.portfolio), parse_date(args.as_of) if args.as_of else None)
    print(f"[{v.kind}] as of {v.as_of}  NAV {fmt_money(v.nav)}  cash {fmt_money(v.cash)} (available {fmt_money(v.available_cash)})")
    if v.missing_prices:
        print("  missing prices: " + ", ".join(v.missing_prices) + " (run `eqm prices refresh`)")
    for h in v.holdings:
        print(f"  {h.symbol:<7} {h.shares:>14,.4f} @ {h.price} ({h.price_date}) = {fmt_money(h.market_value):>14} "
              f"{fmt_pct(h.weight):>7}  basis {fmt_money(h.cost_basis):>14}  unrealized {fmt_money(h.unrealized_gain)}")
    print(f"  realized {fmt_money(v.realized_gain)} (unknown-basis lots: {v.realized_unknown_lots})  dividends {fmt_money(v.dividends)}  fees {fmt_money(v.fees)}")
    print("  sector weights: " + ", ".join(f"{k} {fmt_pct(w)}" for k, w in sorted(v.sector_weights.items(), key=lambda x: -x[1])))
    for i in v.open_issues:
        print(f"  ISSUE {i['severity']} {i['issue_type']}: {i['detail_json'][:120]} ({i['id']})")


def cmd_lots(args):
    from .ledger.views import portfolio_view
    app = _app(args)
    v = portfolio_view(app, _pf(app, args.portfolio))
    print("Informational lot report (FIFO). Not tax advice; unknown basis/acquisition dates stay unknown.")
    for h in v.holdings:
        for l in h.lots:
            hp = "unknown" if l["acquired"] is None else ("long" if (v.as_of - l["acquired"]).days > 365 else "short")
            print(f"  {h.symbol:<7} lot {l['lot_id'][:14]} acquired {l['acquired'] or 'unknown'} qty {l['quantity']:.6f} "
                  f"basis {fmt_money(l['cost_total'])} holding period {hp}")
    for g in v.realized:
        print(f"  realized {g.sell_date} qty {g.quantity:.6f} proceeds {fmt_money(g.proceeds)} cost {fmt_money(g.cost)} "
              f"gain {fmt_money(g.gain)} ({g.holding_period})")


def cmd_reconcile(args):
    from .ledger.reconcile import check_provider_actions
    from .ledger.store import open_issues, resolve_issue
    app = _app(args)
    if args.resolve:
        resolve_issue(app, args.resolve, args.note or "", "ACKNOWLEDGED" if args.acknowledge else "RESOLVED")
        print("updated")
        return
    acct = _acct(app, args.account)
    found = check_provider_actions(app, acct, cal.latest_completed_session(app.now()))
    print(f"{len(found)} provider corporate-action discrepancy(ies)")
    for i in open_issues(app, [acct]):
        print(f"  {i['id']} {i['severity']} {i['issue_type']} {i['detail_json'][:140]}")


def cmd_security(args):
    from .data.securities import register_security
    app = _app(args)
    if args.sec:
        from .data import sec
        sid = sec.register_from_ticker(app, sec.make_client(app), args.symbol)
    else:
        sid = register_security(app.conn, app.now_iso(), args.symbol, security_type="ETF" if args.etf else None)
    print(sid)


def cmd_prices(args):
    from .data.prices import provider_from_settings, refresh_prices, CsvPriceProvider
    app = _app(args)
    if args.symbols:
        sids = [_sid(app, s) for s in args.symbols]
    else:
        from .monitoring.jobs import _tracked
        sids = _tracked(app, [_pf(app, args.portfolio)]) if (args.portfolio or app.settings.active_portfolio) else []
        for b in app.settings.benchmarks:
            from .data.securities import register_security
            sids.append(register_security(app.conn, app.now_iso(), b, security_type="ETF"))
    provider = CsvPriceProvider(args.csv_dir) if args.csv_dir else provider_from_settings(app)
    for sid, r in refresh_prices(app, sorted(set(sids)), provider, lookback_days=args.lookback).items():
        sym = app.conn.execute("SELECT symbol FROM security WHERE id=?", (sid,)).fetchone()["symbol"]
        print(f"  {sym:<7} {'ok latest ' + str(r['latest']) if r['ok'] else 'FAILED ' + r['error']}")


def cmd_sec(args):
    from .data import sec
    from .research.fundamentals import ingest_companyfacts
    app = _app(args)
    client = sec.make_client(app)
    sid = sec.register_from_ticker(app, client, args.symbol)
    iss = app.conn.execute("SELECT issuer_id FROM security WHERE id=?", (sid,)).fetchone()["issuer_id"]
    res = sec.sync_filings(app, client, iss)
    print(f"filings: {len(res.new_document_ids)} new; latest accession {res.latest_accession}")
    raw, rid = sec.fetch_companyfacts(app, client, iss)
    print("facts:", ingest_companyfacts(app, iss, raw, rid))
    if args.docs:
        for form in ("10-K", "10-Q"):
            d = app.conn.execute("SELECT id FROM source_document WHERE issuer_id=? AND doc_type=? ORDER BY public_at DESC LIMIT 1",
                                 (iss, form)).fetchone()
            if d:
                print(f"{form} text: {sec.fetch_document_text(app, client, d['id'])} passages")


def cmd_screen(args):
    from .research.screening import run_screen
    app = _app(args)
    sids = [_sid(app, s) for s in args.symbols]
    run_id, rows = run_screen(app, sids, app.now(), label="CURRENT")
    print(f"screen {run_id} (a research shortlist, not a valuation or recommendation)")
    for r in sorted(rows, key=lambda r: (r.rank or 999, r.symbol)):
        print(f"  {r.symbol:<7} rank {r.rank or '-':>3} score {r.score and round(float(r.score), 3)} quality "
              f"{r.quality_score and round(float(r.quality_score), 3)} value {r.value_score and round(float(r.value_score), 3)} "
              f"{'EXCLUDED ' + r.exclusion_reason if r.exclusion_reason else ''} {r.notes.get('score', '')}")


def cmd_valuation(args):
    from .research.fundamentals import FactView
    from .valuation.builder import MissingInputs, build_scenarios
    from .valuation.dcf import ScenarioInputs, reverse_dcf
    from .valuation.store import approve_valuation, create_valuation, latest_valuation
    from .data.prices import price_on_or_before
    app = _app(args)
    sid = _sid(app, args.symbol)
    if args.action == "build":
        iss = app.conn.execute("SELECT issuer_id FROM security WHERE id=?", (sid,)).fetchone()["issuer_id"]
        if args.inputs:
            data = yaml.safe_load(Path(args.inputs).read_text())
            scen = {k: ScenarioInputs.model_validate(v) for k, v in data.items()}
        else:
            try:
                scen = build_scenarios(FactView(app, iss, app.now()), app.policy.valuation)
            except MissingInputs as exc:
                raise SystemExit(f"cannot value: {exc}")
        vid = create_valuation(app, sid, scen, evidence_as_of=app.now(), author="USER" if args.inputs else "ENGINE",
                               change_reason=args.reason or "")
        print(vid)
        if args.dump:
            Path(args.dump).write_text(yaml.safe_dump(json.loads(to_json({k: v.model_dump(mode='json') for k, v in scen.items()})),
                                                      sort_keys=False))
            print(f"inputs written to {args.dump} (edit, then `eqm valuation build {args.symbol} --inputs {args.dump}`)")
    elif args.action == "approve":
        v = latest_valuation(app, sid)
        approve_valuation(app, v.id, downside_reviewed=args.downside_reviewed, note=args.note or "",
                          accept_assumptions=args.accept_assumptions)
        print(f"approved {v.id} (downside reviewed: {args.downside_reviewed})")
    elif args.action == "show":
        v = latest_valuation(app, sid)
        if not v:
            raise SystemExit("no valuation")
        px = price_on_or_before(app, sid, cal.latest_completed_session(app.now()))
        print(f"{args.symbol} valuation v{v.version_no} ({'approved' if v.approved else 'NOT approved'}) created {v.created_at}")
        for sc in ("bear", "base", "bull"):
            o = v.outputs[sc]
            print(f"  {sc:<5} value/share {Decimal(o['value_per_share']):,.2f}  EV {Decimal(o['enterprise_value']):,.0f}  "
                  f"terminal share {fmt_pct(Decimal(o['terminal_share']))}  warnings {o['warnings']}")
        if px:
            from .valuation.dcf import margin_of_safety
            print(f"  price {px[1]} ({px[0]}): margin of safety vs base {fmt_pct(margin_of_safety(px[1], v.base, v.base_meaningful))}")
        flags = v.inputs["base"].get("review_flags")
        if flags:
            print("  REVIEW FLAGS:", "; ".join(flags))
        print("  Discount rate and value gaps are not forecast returns.")
    elif args.action == "reverse":
        v = latest_valuation(app, sid)
        px = price_on_or_before(app, sid, cal.latest_completed_session(app.now()))
        if not (v and px):
            raise SystemExit("need a valuation and a price")
        r = reverse_dcf(ScenarioInputs.model_validate(v.inputs["base"]), px[1], args.variable, cap=app.policy.valuation.terminal_growth_cap)
        print(f"reverse DCF for {args.variable} at price {px[1]}: {r.status} implied={r.implied_value and round(float(r.implied_value), 4)}")
        print(f"  bounds {r.bounds}, values at bounds {r.value_at_bounds}; {r.note}")


def cmd_thesis(args):
    from .research import thesis as th
    app = _app(args)
    if args.action == "template":
        print(Path("config/thesis.example.yaml").read_text())
        return
    if args.action == "approve":
        th.approve_version(app, args.version_id, note=args.note or "", acknowledge_unverified=args.acknowledge_unverified)
        print("approved")
        return
    sid = _sid(app, args.symbol)
    if args.action == "create":
        content = th.ThesisContent.model_validate(yaml.safe_load(Path(args.file).read_text()))
        vid = th.create_version(app, sid, content, change_reason=args.reason or "initial thesis", author="USER")
        v = [x for x in th.history(app, sid) if x.id == vid][0]
        print(f"{vid} (draft; approve with `eqm thesis approve --version-id {vid}`)")
        for c in v.claims:
            print(f"  [{c['claim_type']}/{c['verification']}] {c['text']} {c['verification_detail'] or ''}")
    elif args.action == "draft":
        from .llm.service import draft_thesis, provider_from_settings
        res = draft_thesis(app, provider_from_settings(app), sid)
        if res["content"] is None:
            raise SystemExit(f"no valid draft ({res.get('error')}); see llm_call {res['llm_call_id']}")
        out = Path(args.out or f"thesis_draft_{args.symbol}.yaml")
        out.write_text(yaml.safe_dump(json.loads(res["content"].model_dump_json()), sort_keys=False))
        print(f"LLM draft written to {out}. It is NOT approved. Claim checks:")
        for c in res["claims"]:
            print(f"  [{c['type']}/{c['status']}] {c['text'][:100]} {'; '.join(c['details'])}")
    elif args.action == "show":
        for v in th.history(app, sid):
            print(f"v{v.version_no} {v.author} created {v.created_at} {'approved ' + v.approved_at if v.approved else 'DRAFT'}: {v.change_reason}")
    elif args.action == "assess":
        cond = args.condition_id
        th.record_assessment(app, "INVALIDATION", cond, args.state, verified=True, assessor="USER", evidence={"note": args.note})
        print("assessment recorded")


def cmd_watchlist(args):
    from .decisions.recommend import set_watchlist
    app = _app(args)
    set_watchlist(app, _sid(app, args.symbol), args.status, args.note)
    print("ok")


def cmd_review(args):
    from .decisions.recommend import get, review_portfolio
    from .reporting.reports import export_decisions, portfolio_review_md, write_json, write_report
    app = _app(args)
    pf = _pf(app, args.portfolio)
    ids = review_portfolio(app, pf)
    for rid in ids:
        r = get(app, rid)
        print(f"  {r['payload']['symbol']:<7} {r['action']:<6} business {r['business_assessment']:<9} "
              f"{'PREVIEW ' if r['is_preview'] else ''}{r['explanation'][:110]}")
    md_path, html_path = write_report(app, "portfolio_review", portfolio_review_md(app, pf))
    jp = write_json(app, "decisions_export", export_decisions(app, pf))
    print(f"report: {md_path} / {html_path}; export: {jp}")


def cmd_decide(args):
    from .decisions.recommend import record_decision
    app = _app(args)
    subject = ("ALLOCATION", args.allocation) if args.allocation else ("RECOMMENDATION", args.recommendation)
    print(record_decision(app, subject[0], subject[1], args.decision, rationale=args.rationale or "",
                          override_action=args.override_action))


def cmd_allocate(args):
    from .decisions.allocation import load, propose
    from .reporting.reports import allocation_md, write_report
    app = _app(args)
    p = propose(app, _pf(app, args.portfolio), hypothetical_contribution=_d(args.hypothetical),
                conditional_sale_proceeds=_d(args.conditional_sales))
    md = allocation_md(app, load(app, p.id))
    print(md)
    print("report:", write_report(app, "allocation", md)[0])


def cmd_jobs(args):
    from .monitoring import scheduler as sch
    from .monitoring.jobs import handlers
    app = _app(args)
    if args.action == "status":
        for s in sch.status(app):
            lr = s["last_run"] or {}
            print(f"  {s['job']:<19} last {lr.get('started_at', 'never')} {lr.get('status', '')}  next {s['next_run']}"
                  + (f"  MISSED {s['latest_due_not_run']}" if s["latest_due_not_run"] else ""))
    elif args.action == "run-due":
        for r in sch.run_due(app, handlers()):
            print(f"  {r['job']}: {r['status']}" + (f" ({r.get('error')})" if r.get("error") else ""))
    elif args.action == "run":
        spec = next(s for s in sch.DEFAULT_JOBS if s.name == args.name)
        r = sch.run_instance(app, spec, sch.latest_due(spec, app.now()) or app.now(), handlers()[spec.name], force=args.force)
        _print({k: v for k, v in r.items() if k != "detail"})
        if r.get("detail"):
            _print(r["detail"])


def cmd_serve(args):
    from .monitoring import scheduler as sch
    from .monitoring.delivery import deliver_pending
    from .monitoring.jobs import handlers
    print("equity-monitor scheduler running (polling; not real-time). Ctrl-C to stop.", flush=True)
    sch.serve(lambda: _app(args), handlers(), poll_seconds=args.poll, deliver=deliver_pending)


def cmd_alerts(args):
    from .monitoring.alerts import acknowledge, inbox, snooze
    from .monitoring.delivery import deliver_pending, requeue
    app = _app(args)
    if args.action == "list":
        for a in inbox(app, include_acknowledged=args.all):
            print(f"  {a['id']} {a['severity']:<8} {a['kind']:<18} {a['status']:<12} {a['created_at'][:16]} {a['title']}")
    elif args.action == "show":
        print(app.conn.execute("SELECT body_md FROM alert WHERE id=?", (args.alert_id,)).fetchone()["body_md"])
    elif args.action == "ack":
        acknowledge(app, args.alert_id, args.note)
    elif args.action == "snooze":
        snooze(app, args.alert_id, app.now() + timedelta(days=args.days), args.note)
    elif args.action == "deliver":
        print(deliver_pending(app))
    elif args.action == "requeue":
        requeue(app, args.alert_id, args.note or "")
    elif args.action in ("useful", "not-useful"):
        from .monitoring.alerts import record_feedback
        record_feedback(app, args.alert_id, args.action == "useful", args.note)


def cmd_health(args):
    from .reporting.reports import health_md, write_report
    app = _app(args)
    md = health_md(app)
    print(md)
    write_report(app, "system_health", md)


def cmd_report(args):
    from .reporting import reports
    app = _app(args)
    pf = _pf(app, args.portfolio)
    if args.kind == "weekly":
        md = reports.weekly_digest_md(app, pf)
    elif args.kind == "portfolio":
        md = reports.portfolio_review_md(app, pf)
    elif args.kind == "company":
        md = reports.company_md(app, pf, _sid(app, args.symbol))
    elif args.kind == "market":
        from .market.snapshot import snapshot_as_of
        md = reports.market_context_md(app, snapshot_as_of(app, app.now()))
    else:
        print(reports.write_json(app, "decisions_export", reports.export_decisions(app, pf)))
        return
    print(reports.write_report(app, f"{args.kind}{'_' + args.symbol if args.symbol else ''}", md)[0])


def cmd_performance(args):
    from .evaluation.performance import performance, process_metrics
    app = _app(args)
    pf = _pf(app, args.portfolio)
    end = parse_date(args.end) if args.end else cal.latest_completed_session(app.now())
    start = parse_date(args.start) if args.start else end - timedelta(days=365)
    _print(performance(app, pf, start, end))
    _print({"process_quality": process_metrics(app)})


def cmd_demo(args):
    from .app import open_app as _open
    from .decisions.allocation import load, propose
    from .decisions.recommend import review_portfolio
    from .evaluation.performance import performance
    from .fixtures import AS_OF, build_demo
    from .monitoring.jobs import JobContext, weekly_digest
    from .monitoring import scheduler as sch
    from .reporting import reports
    from .util import Clock
    home = Path(args.home_demo)
    if (home / "equity_monitor.sqlite").exists():
        raise SystemExit(f"{home} already has a database; pick another --home-demo")
    app = _open(home, clock=Clock(AS_OF), policy_path=args.policy, settings_path=None)
    out = Path(args.out)
    d = build_demo(app)
    pf = d["portfolio_id"]
    from .fixtures import build_market_fixture
    from .market.snapshot import load_snapshot
    from .market.exposures import Exposure, approve_profile, create_profile, current_profile
    zz = d["securities"]["ZZNEW"]["security_id"]
    prof = current_profile(app, zz)[1]
    prof = prof.with_exposure(Exposure(
        factor="REFINANCING", direction="NEGATIVE", magnitude="HIGH", basis="ANALYST_ASSUMPTION", valuation_assumption="wacc",
        mechanism="ILLUSTRATIVE: large bond maturity next year must be refinanced"))
    approve_profile(app, create_profile(app, zz, prof, change_reason="FIXTURE: owner flags refinancing risk", author="FIXTURE",
                                        label="FIXTURE"))
    snap = build_market_fixture(app, hy_level=Decimal("6.2"))    # FIXTURE market with a credit-tightening flag
    review_portfolio(app, pf)
    reports.write_report(app, "market_context", reports.market_context_md(app, load_snapshot(app, snap)), out_dir=out)
    reports.write_report(app, "portfolio_review", reports.portfolio_review_md(app, pf), out_dir=out)
    prop = propose(app, pf)
    reports.write_report(app, "allocation", reports.allocation_md(app, load(app, prop.id)), out_dir=out)
    for sym in ("ZZADD", "ZZEXT"):
        reports.write_report(app, f"company_{sym}", reports.company_md(app, pf, d["securities"][sym]["security_id"]), out_dir=out)
    spec = sch.DEFAULT_JOBS[1]
    sch.run_instance(app, spec, sch.latest_due(spec, app.now()), lambda a, r, w: weekly_digest(a, r, w, JobContext()))
    reports.write_report(app, "weekly_digest", reports.weekly_digest_md(app, pf), out_dir=out)
    reports.write_report(app, "system_health", reports.health_md(app), out_dir=out)
    reports.write_json(app, "decisions_export", reports.export_decisions(app, pf), out_dir=out)
    perf = performance(app, pf, date(2026, 6, 1), AS_OF.date())
    reports.write_json(app, "performance", perf, out_dir=out)
    print(f"FIXTURE demo built in {home}; reports in {out}/{AS_OF.date()}/")


def cmd_dashboard(args):
    from .dashboard.server import run
    run(args, port=args.port)


def cmd_backup(args):
    from .ops import backup, restore
    if args.action == "create":
        print(backup(_app(args), args.dest))
    else:
        print(restore(args.archive, args.home or "var", force=args.force))


def cmd_policy(args):
    app = _app(args)
    if args.action == "show":
        _print({"status": app.policy.status, "hash": app.policy.content_hash(), "policy": app.policy.model_dump(mode="json")})
    else:
        from .decisions.recommend import record_decision
        pid = app.policy_version_id()
        record_decision(app, "POLICY", pid, "ACCEPT", rationale=args.note or "owner approved policy")
        print(f"approval recorded for policy version {pid}. Now set `status: APPROVED` in config/policy.yaml "
              "(the content hash will be re-registered; approval refers to this exact content).")


def cmd_market(args):
    from .market import snapshot as snap_mod
    from .market.sources import sources_markdown
    from .reporting.reports import market_context_md, write_report
    app = _app(args)
    if args.action == "sources":
        print(sources_markdown())
    elif args.action == "refresh":
        from .data.prices import provider_from_settings
        from .monitoring.jobs import JobContext, _tracked, refresh_market_context
        pids = [_pf(app, args.portfolio)] if (args.portfolio or app.settings.active_portfolio) else []
        out = refresh_market_context(app, JobContext(), _tracked(app, pids) if pids else [], provider_from_settings(app), None)
        _print(out)
    elif args.action == "show":
        s = snap_mod.snapshot_as_of(app, app.now())
        if s is None:
            raise SystemExit("no market snapshot yet: run `eqm market refresh`")
        md = market_context_md(app, s)
        print(md)
        print("report:", write_report(app, "market_context", md)[0])
    elif args.action == "note":
        from .market.external import add_external_observation
        oid, status = add_external_observation(app, _sid(app, args.symbol), source_name=args.source, text=args.text, url=args.url,
                                               published_at=datetime.fromisoformat(args.published) if args.published else app.now(),
                                               passage_id=args.passage_id, quote=args.quote, fact_id=args.fact_id)
        print(f"{oid}: {status} (only VERIFIED claims count as facts; others are context)")
    elif args.action == "explain":
        from .llm.service import explain_cluster, provider_from_settings
        _print(explain_cluster(app, provider_from_settings(app), args.recommendation, args.cluster))
    elif args.action == "lookthrough":
        from .market.lookthrough import portfolio_market_exposure
        _print(portfolio_market_exposure(app, _pf(app, args.portfolio), cal.latest_completed_session(app.now())))


def cmd_exposure(args):
    from .market import exposures as ex
    app = _app(args)
    if args.action == "approve":
        ex.approve_profile(app, args.version_id, note=args.note or "", acknowledge_unverified=args.acknowledge_unverified)
        print("approved")
        return
    sid = _sid(app, args.symbol)
    if args.action == "draft":
        prof = ex.draft_default_profile(app, sid)
        out = Path(args.out or f"exposure_{args.symbol}.yaml")
        out.write_text(yaml.safe_dump(json.loads(prof.model_dump_json()), sort_keys=False))
        print(f"DRAFT written to {out}: review every exposure (evidence or ANALYST_ASSUMPTION), then "
              f"`eqm exposure create {args.symbol} --file {out} --reason ...`")
    elif args.action == "create":
        prof = ex.ExposureProfile.model_validate(yaml.safe_load(Path(args.file).read_text()))
        vid = ex.create_profile(app, sid, prof, change_reason=args.reason or "", author="USER")
        print(f"{vid} (not approved; approve with `eqm exposure approve --version-id {vid}`)")
    elif args.action == "show":
        for v in ex.profile_history(app, sid):
            print(f"v{v['version_no']} {v['author']} {v['created_at'][:16]} {'approved ' + v['approved_at'][:16] if v['approved_at'] else 'DRAFT'}: "
                  f"{v['change_reason']}")
            for e, ver in zip(json.loads(v["content_json"])["exposures"], json.loads(v["verification_json"])):
                print(f"   {e['factor']:<24} {e['direction']:<8} {e['magnitude']:<7} [{ver['status']}] {e['mechanism'][:80]}")


def cmd_research(args):
    app = _app(args)
    if args.close:
        app.conn.execute("UPDATE research_task SET status=?, closed_at=? WHERE id=?",
                         ("DISMISSED" if args.dismiss else "DONE", app.now_iso(), args.close))
        print("updated")
        return
    for r in app.conn.execute("SELECT t.*, s.symbol FROM research_task t LEFT JOIN security s ON s.id=t.security_id "
                              "WHERE t.status='OPEN' ORDER BY t.created_at"):
        print(f"  {r['id']} {r['symbol'] or '':<7} {r['reason']:<28} {r['created_at'][:16]} {json.loads(r['detail_json']).get('detail', '')[:90]}")


def cmd_evaluate(args):
    from .evaluation.augmented import compare
    app = _app(args)
    _print(compare(app, _pf(app, args.portfolio)))


def cmd_paper(args):
    from .evaluation.paper import paper_execute_allocation
    app = _app(args)
    print(paper_execute_allocation(app, args.proposal, _pf(app, args.paper_portfolio), args.variant))


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eqm", description="Fundamental investing research & portfolio monitor (no trading).")
    p.add_argument("--home", default=os.environ.get("EQM_HOME", "var"), help="data directory (default ./var or $EQM_HOME)")
    p.add_argument("--policy", default=None, help="policy YAML (default config/policy.yaml if present)")
    p.add_argument("--settings", default=None, help="user settings YAML (default config/user.yaml if present)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create DB and copy example config").set_defaults(fn=cmd_init)

    s = sub.add_parser("portfolio", help="portfolios and accounts")
    s.add_argument("action", choices=["create", "add-account", "list"])
    s.add_argument("name", nargs="?")
    s.add_argument("--kind", default="ACTUAL", choices=["ACTUAL", "PAPER", "FIXTURE", "HYPOTHETICAL"])
    s.add_argument("--portfolio")
    s.add_argument("--broker")
    s.add_argument("--tax-status", default="UNKNOWN", choices=["TAXABLE", "TAX_DEFERRED", "TAX_FREE", "UNKNOWN"])
    s.add_argument("--settlement-days", type=int, default=1)
    s.set_defaults(fn=cmd_portfolio)

    s = sub.add_parser("import", help="import transactions CSV or a brokerage snapshot")
    s.add_argument("kind", choices=["transactions", "snapshot"])
    s.add_argument("account")
    s.add_argument("file")
    s.add_argument("--as-of", help="snapshot date YYYY-MM-DD")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("ledger", help="manual entry, reversal, listing")
    s.add_argument("action", choices=["add", "reverse", "list"])
    s.add_argument("--account")
    s.add_argument("--type")
    s.add_argument("--date")
    s.add_argument("--symbol")
    s.add_argument("--security-type")
    for f in ("quantity", "price", "fees", "amount", "cost-basis", "split-from", "split-to", "external-id", "note"):
        s.add_argument(f"--{f}")
    s.add_argument("--event-id")
    s.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_ledger)

    s = sub.add_parser("show", help="portfolio view")
    s.add_argument("--portfolio")
    s.add_argument("--as-of")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("lots", help="informational lot & holding-period report")
    s.add_argument("--portfolio")
    s.set_defaults(fn=cmd_lots)

    s = sub.add_parser("reconcile", help="check provider corporate actions; list/resolve issues")
    s.add_argument("--account")
    s.add_argument("--resolve")
    s.add_argument("--acknowledge", action="store_true")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("security", help="register a security")
    s.add_argument("symbol")
    s.add_argument("--etf", action="store_true")
    s.add_argument("--sec", action="store_true", help="look up issuer/CIK in the SEC ticker map")
    s.set_defaults(fn=cmd_security)

    s = sub.add_parser("prices", help="refresh prices")
    s.add_argument("action", choices=["refresh"])
    s.add_argument("--portfolio")
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--csv-dir")
    s.add_argument("--lookback", type=int, default=400)
    s.set_defaults(fn=cmd_prices)

    s = sub.add_parser("sec", help="sync SEC filings + XBRL facts for a ticker")
    s.add_argument("action", choices=["sync"])
    s.add_argument("symbol")
    s.add_argument("--docs", action="store_true", help="also fetch latest 10-K/10-Q text passages")
    s.set_defaults(fn=cmd_sec)

    s = sub.add_parser("screen", help="screen securities (research shortlist)")
    s.add_argument("symbols", nargs="+")
    s.set_defaults(fn=cmd_screen)

    s = sub.add_parser("valuation", help="build/approve/show/reverse DCF valuations")
    s.add_argument("action", choices=["build", "approve", "show", "reverse"])
    s.add_argument("symbol")
    s.add_argument("--inputs", help="YAML with bear/base/bull ScenarioInputs")
    s.add_argument("--dump", help="write the generated inputs to YAML for editing")
    s.add_argument("--reason")
    s.add_argument("--downside-reviewed", action="store_true")
    s.add_argument("--accept-assumptions", action="store_true", help="explicitly accept flagged assumptions (listed by `valuation show`)")
    s.add_argument("--note")
    s.add_argument("--variable", default="revenue_growth", choices=["revenue_growth", "ebit_margin", "wacc", "terminal_growth"])
    s.set_defaults(fn=cmd_valuation)

    s = sub.add_parser("thesis", help="thesis versions")
    s.add_argument("action", choices=["template", "create", "draft", "approve", "show", "assess"])
    s.add_argument("symbol", nargs="?")
    s.add_argument("--file")
    s.add_argument("--reason")
    s.add_argument("--version-id")
    s.add_argument("--out")
    s.add_argument("--note")
    s.add_argument("--condition-id")
    s.add_argument("--state", choices=["TRIGGERED", "NOT_TRIGGERED", "AMBIGUOUS"])
    s.add_argument("--acknowledge-unverified", action="store_true",
                   help="approve although some FACT claims are only SOURCE_MATCHED/UNVERIFIED (listed by `thesis show`)")
    s.set_defaults(fn=cmd_thesis)

    s = sub.add_parser("watchlist", help="set watchlist status")
    s.add_argument("symbol")
    s.add_argument("status", choices=["RESEARCH", "APPROVED", "REJECTED", "ARCHIVED"])
    s.add_argument("--note")
    s.set_defaults(fn=cmd_watchlist)

    s = sub.add_parser("review", help="generate recommendations + portfolio report")
    s.add_argument("--portfolio")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("decide", help="record your decision on a recommendation or allocation")
    s.add_argument("decision", choices=["ACCEPT", "REJECT", "OVERRIDE", "DEFER"])
    s.add_argument("--recommendation")
    s.add_argument("--allocation")
    s.add_argument("--override-action", choices=["ADD", "HOLD", "TRIM", "EXIT", "REVIEW"])
    s.add_argument("--rationale")
    s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("allocate", help="monthly allocation proposal")
    s.add_argument("--portfolio")
    s.add_argument("--hypothetical", help="hypothetical contribution amount (labelled HYPOTHETICAL)")
    s.add_argument("--conditional-sales", help="include conditional sale proceeds (labelled)")
    s.set_defaults(fn=cmd_allocate)

    s = sub.add_parser("jobs", help="scheduler: status, run-due, run")
    s.add_argument("action", choices=["status", "run-due", "run"])
    s.add_argument("name", nargs="?")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("serve", help="run the scheduler loop (persistent process)")
    s.add_argument("--poll", type=int, default=60)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("alerts", help="local inbox and delivery")
    s.add_argument("action", choices=["list", "show", "ack", "snooze", "deliver", "requeue", "useful", "not-useful"])
    s.add_argument("alert_id", nargs="?")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--note")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_alerts)

    sub.add_parser("health", help="system health report").set_defaults(fn=cmd_health)

    s = sub.add_parser("report", help="write a dated report")
    s.add_argument("kind", choices=["weekly", "portfolio", "company", "export", "market"])
    s.add_argument("--portfolio")
    s.add_argument("--symbol")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("performance", help="performance vs contribution-matched benchmarks")
    s.add_argument("--portfolio")
    s.add_argument("--start")
    s.add_argument("--end")
    s.set_defaults(fn=cmd_performance)

    s = sub.add_parser("demo", help="build the labelled FIXTURE demo and its reports")
    s.add_argument("--home-demo", default="var/demo")
    s.add_argument("--out", default="reports/equity/demo")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("dashboard", help="local web dashboard")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("backup", help="backup/restore")
    s.add_argument("action", choices=["create", "restore"])
    s.add_argument("--dest", default="backups")
    s.add_argument("--archive")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("market", help="market context: refresh, show snapshot, sources, notes, explanations, look-through")
    s.add_argument("action", choices=["refresh", "show", "sources", "note", "explain", "lookthrough"])
    s.add_argument("symbol", nargs="?")
    s.add_argument("--portfolio")
    s.add_argument("--source", default="owner note")
    s.add_argument("--text")
    s.add_argument("--url")
    s.add_argument("--published", help="ISO timestamp with timezone")
    s.add_argument("--passage-id")
    s.add_argument("--quote")
    s.add_argument("--fact-id")
    s.add_argument("--recommendation")
    s.add_argument("--cluster")
    s.set_defaults(fn=cmd_market)

    s = sub.add_parser("exposure", help="company exposure profiles (draft/create/approve/show)")
    s.add_argument("action", choices=["draft", "create", "approve", "show"])
    s.add_argument("symbol", nargs="?")
    s.add_argument("--file")
    s.add_argument("--out")
    s.add_argument("--reason")
    s.add_argument("--version-id")
    s.add_argument("--note")
    s.add_argument("--acknowledge-unverified", action="store_true",
                   help="approve although some EVIDENCED exposures are only SOURCE_MATCHED/UNVERIFIED")
    s.set_defaults(fn=cmd_exposure)

    s = sub.add_parser("research", help="open research tasks raised by observations")
    s.add_argument("--close")
    s.add_argument("--dismiss", action="store_true")
    s.set_defaults(fn=cmd_research)

    s = sub.add_parser("evaluate", help="fundamental-only baseline vs augmented system")
    s.add_argument("--portfolio")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("paper", help="paper-execute an allocation variant (requires FROZEN policy)")
    s.add_argument("--proposal", required=True)
    s.add_argument("--paper-portfolio", required=True)
    s.add_argument("--variant", choices=["augmented", "baseline"], required=True)
    s.set_defaults(fn=cmd_paper)

    s = sub.add_parser("policy", help="show or approve the decision policy")
    s.add_argument("action", choices=["show", "approve"])
    s.add_argument("--note")
    s.set_defaults(fn=cmd_policy)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
