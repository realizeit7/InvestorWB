"""Job handlers. Stages are separate functions: collect -> detect -> analyze -> alert -> deliver.

A failed collection step is recorded (source_check + data_quality_issue + HEALTH event) and the
job finishes PARTIAL; it never reports "nothing changed" after a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..app import App
from ..data import calendar as cal
from ..data.http import ProviderError
from ..data.prices import PriceProvider, provider_from_settings, refresh_prices
from ..data.sec import SecConfigError, fetch_companyfacts, filing_severity as _filing_severity, make_client, sync_filings
from ..db.core import all_rows, one
from ..decisions import recommend as rec_mod
from ..decisions.allocation import propose
from ..ledger.store import portfolio_by_name
from ..research.fundamentals import ingest_companyfacts
from ..reporting import reports
from ..util import from_json, iso_utc
from .alerts import create_alert, record_event
from .delivery import deliver_pending


@dataclass
class JobContext:
    price_provider: PriceProvider | None = None
    sec_client: object | None = None           # HttpClient or a fake with .get
    submissions: dict = field(default_factory=dict)   # issuer_id -> raw submissions JSON (tests)
    portfolios: list[str] | None = None
    refresh_market_series: bool = True               # FRED / FINRA (network); tests inject fetchers or disable
    fred_fetch: object | None = None
    finra_si_fetch: object | None = None
    regsho_fetch: object | None = None


def _portfolios(app: App, ctx: JobContext) -> list[str]:
    if ctx.portfolios:
        return [portfolio_by_name(app, p)["id"] for p in ctx.portfolios]
    if app.settings.active_portfolio:
        return [portfolio_by_name(app, app.settings.active_portfolio)["id"]]
    return [r["id"] for r in all_rows(app.conn, "SELECT id FROM portfolio WHERE kind IN ('ACTUAL','FIXTURE') ORDER BY created_at")]


def _tracked(app: App, portfolio_ids: list[str]) -> list[str]:
    from ..ledger.views import portfolio_view
    sids: set[str] = set()
    for p in portfolio_ids:
        sids |= {h.security_id for h in portfolio_view(app, p, cal.latest_completed_session(app.now())).holdings}
    sids |= {r["security_id"] for r in all_rows(app.conn, "SELECT security_id FROM watchlist_entry WHERE status IN ('APPROVED','RESEARCH')")}
    # finder shortlists are evaluated prospectively, so their prices must keep updating for the longest horizon
    since = iso_utc(app.now() - timedelta(days=400))
    sids |= {r["security_id"] for r in all_rows(app.conn, "SELECT DISTINCT c.security_id FROM finder_candidate c JOIN finder_run f "
                                                          "ON f.id=c.run_id WHERE c.stage='SHORTLIST' AND f.created_at>=? "
                                                          "AND c.security_id IS NOT NULL", (since,))}
    from ..data.securities import register_security
    for b in app.settings.benchmarks:                 # contribution-matched benchmarks need daily prices too
        sids.add(register_security(app.conn, app.now_iso(), b, security_type="ETF"))
    return sorted(sids)


def _research_portfolios(app: App, pids: list[str]) -> list[str]:
    return [p for p in pids if rec_mod.company_research_scope(app, p)[0]]


def daily_refresh(app: App, run_id: str, scheduled_for: datetime, ctx: JobContext | None = None) -> dict:
    ctx = ctx or JobContext()
    pids = _portfolios(app, ctx)
    sids = _tracked(app, pids)
    failures: list[str] = []
    detail: dict = {"securities": len(sids), "portfolios": pids}
    # 1. collect prices
    provider = ctx.price_provider or provider_from_settings(app)
    pr = refresh_prices(app, sids, provider, job_run_id=run_id)
    failures += [f"prices {sid}: {r['error']}" for sid, r in pr.items() if not r["ok"]]
    # 2. collect filings (SEC) for tracked issuers
    rows = all_rows(app.conn, f"SELECT DISTINCT s.issuer_id, i.name FROM security s JOIN issuer i ON i.id=s.issuer_id "
                              f"WHERE s.id IN ({','.join('?' for _ in sids)}) AND i.cik IS NOT NULL", sids) if sids else []
    from ..data.prices import record_check
    issuers = []
    for r in rows:
        if r["name"].startswith("FIXTURE "):
            # fictional issuers have no SEC filings; record an explicit fixture check so freshness is honest
            record_check(app, "fixture", r["issuer_id"], "FILINGS", True, "FIXTURE", None, run_id)
        else:
            issuers.append(r["issuer_id"])
    new_events = []
    client = ctx.sec_client
    if issuers and client is None and not ctx.submissions:
        try:
            client = make_client(app)
        except SecConfigError as exc:
            failures.append(f"SEC not configured: {exc}")
            for iid in issuers:
                record_check(app, "SEC_EDGAR", iid, "FILINGS", False, None, "SEC user agent not configured", run_id)
            issuers = []
    baseline_cutoff = iso_utc(app.now() - timedelta(days=7))
    for iid in issuers:
        had_prior_check = one(app.conn, "SELECT 1 FROM source_check WHERE subject=? AND check_type='FILINGS' AND success=1",
                              (iid,)) is not None
        try:
            res = sync_filings(app, client, iid, job_run_id=run_id, submissions_json=ctx.submissions.get(iid))
        except ProviderError as exc:
            failures.append(f"filings {iid}: {exc}")
            continue
        need_facts = False
        for did in res.new_document_ids:
            d = one(app.conn, "SELECT * FROM source_document WHERE id=?", (did,))
            need_facts |= d["doc_type"].startswith(("10-K", "10-Q"))
            if not had_prior_check and (d["public_at"] or "") < baseline_cutoff:
                continue   # first sync = baseline backfill; only recent filings raise events
            sev, label = _filing_severity(d["doc_type"], d["items"])
            sec_row = one(app.conn, "SELECT id FROM security WHERE issuer_id=? ORDER BY created_at LIMIT 1", (iid,))
            eid, created = record_event(app, f"filing:{d['accession_no']}:{d['doc_type']}", "NEW_FILING", severity=sev,
                                        verified=True, security_id=sec_row["id"] if sec_row else None, issuer_id=iid,
                                        public_at=d["public_at"], document_id=did, job_run_id=run_id,
                                        payload={"form": d["doc_type"], "items": d["items"], "label": label,
                                                 "url": d["source_url"]})
            if created:
                new_events.append(eid)
        if need_facts and client is not None and not ctx.submissions:
            try:
                raw, rid = fetch_companyfacts(app, client, iid)
                ingest_companyfacts(app, iid, raw, rid)
            except ProviderError as exc:
                failures.append(f"facts {iid}: {exc}")
    # 2b. market context: reference instruments, economic series, positioning data, shared snapshot
    detail["market"] = refresh_market_context(app, ctx, sids, provider, run_id)
    failures += detail["market"].pop("failures")
    # 3. analyze: recommendations for every portfolio
    changed = []
    for p in _research_portfolios(app, pids):          # retirement portfolios: monitored, never company-reviewed
        before = {r["id"] for r in all_rows(app.conn, "SELECT id FROM recommendation WHERE portfolio_id=?", (p,))}
        changed += decision_change_events(app, p, rec_mod.review_portfolio(app, p), before, run_id)
    # 4. alerts
    for eid in new_events + changed:
        _alert_for_event(app, eid, pids)
    for f in failures:
        key = f"health:{scheduled_for.date()}:{f[:60]}"
        eid, created = record_event(app, key, "REFRESH_FAILURE", severity="MATERIAL", verified=True, job_run_id=run_id,
                                    payload={"label": f})
        if created:
            create_alert(app, f"alert:{key}", "HEALTH", f"Refresh failure: {f[:80]}",
                         f"A scheduled refresh failed. Affected recommendations show REVIEW until it succeeds.\n\n- {f}\n- Job run: {run_id}",
                         severity="MATERIAL", event_id=eid, cooldown_group="Refresh failure")
    # 5. deliver (only if enabled/authorized)
    detail.update({"price_results": {k: v.get("ok") for k, v in pr.items()}, "new_events": len(new_events),
                   "action_changes": len(changed), "failures": failures, "delivery": deliver_pending(app)})
    return detail


def decision_change_events(app: App, portfolio_id: str, rec_ids: list[str], before: set[str], run_id: str | None) -> list[str]:
    """One event per NEW recommendation whose action or purchase eligibility differs from its predecessor. Used by every
    job that can create recommendations (daily review, monthly allocation re-validation) so no transition is missed."""
    out = []
    for rid in rec_ids:
        r = rec_mod.get(app, rid)
        if rid in before or not r["previous_recommendation_id"]:
            continue
        prev = one(app.conn, "SELECT action, purchase_eligibility FROM recommendation WHERE id=?",
                   (r["previous_recommendation_id"],))
        action_changed = prev["action"] != r["action"]
        elig_changed = prev["purchase_eligibility"] != r["purchase_eligibility"]
        if not (action_changed or elig_changed):
            continue
        parts = ([f"{prev['action']} → {r['action']}"] if action_changed else []) + \
            ([f"purchases {prev['purchase_eligibility'] or 'UNKNOWN'} → {r['purchase_eligibility']}"] if elig_changed else [])
        eid, created = record_event(
            app, f"{'action' if action_changed else 'eligibility'}:{rid}",
            "ACTION_CHANGE" if action_changed else "ELIGIBILITY_CHANGE",
            severity="CRITICAL" if r["action"] == "EXIT" else "MATERIAL", verified=True,
            security_id=r["security_id"], public_at=r["as_of"], job_run_id=run_id,
            payload={"label": "; ".join(parts), "recommendation_id": rid, "portfolio_id": portfolio_id,
                     "previous_recommendation_id": r["previous_recommendation_id"],
                     "action": {"before": prev["action"], "now": r["action"]},
                     "eligibility": {"before": prev["purchase_eligibility"], "now": r["purchase_eligibility"]}})
        if created:
            out.append(eid)
    return out


def refresh_market_context(app: App, ctx: JobContext, tracked: list[str], provider, run_id: str | None) -> dict:
    from ..market import series as ms
    from ..market.exposures import current_profile
    from ..market.snapshot import build_snapshot, ensure_reference_securities
    failures: list[str] = []
    industry = sorted({p[1].industry_benchmark for sid in tracked if (p := current_profile(app, sid)) and p[1].industry_benchmark})
    refs = ensure_reference_securities(app, industry)
    pr = refresh_prices(app, sorted(set(refs.values())), provider, job_run_id=run_id)
    missing_refs = [s for s, sid in refs.items() if not pr.get(sid, {}).get("ok")]
    out = {"reference_instruments": len(refs), "reference_failures": missing_refs}
    if ctx.refresh_market_series:
        fred = ms.refresh_fred(app, fetch=ctx.fred_fetch, job_run_id=run_id)
        out["fred_failures"] = [k for k, v in fred.items() if not v["ok"]]
        commons = [r for r in all_rows(app.conn, f"SELECT s.symbol FROM security s JOIN issuer i ON i.id=s.issuer_id WHERE s.id IN "
                                                 f"({','.join('?' for _ in tracked)}) AND s.security_type='COMMON' "
                                                 f"AND i.name NOT LIKE 'FIXTURE %'", tracked)] if tracked else []
        syms = [r["symbol"] for r in commons]
        out["short_interest"] = {s_: ms.refresh_short_interest(app, s_, fetch=ctx.finra_si_fetch, job_run_id=run_id)["ok"]
                                 for s_ in syms}
        if syms:
            out["short_volume"] = ms.refresh_short_volume(app, syms, cal.latest_completed_session(app.now()),
                                                          fetch=ctx.regsho_fetch, job_run_id=run_id)
        if out["fred_failures"]:
            failures.append(f"economic series unavailable: {', '.join(out['fred_failures'])}")
    else:
        out["series"] = "skipped (refresh_market_series=False)"
    if missing_refs:
        failures.append(f"reference prices unavailable: {', '.join(missing_refs)}")
    out["snapshot_id"] = build_snapshot(app, app.now(), industry)
    out["failures"] = failures
    return out


def _alert_for_event(app: App, event_id: str, pids: list[str]) -> None:
    e = one(app.conn, "SELECT * FROM detected_event WHERE id=?", (event_id,))
    if e["severity"] == "INFO":
        return
    p = from_json(e["payload_json"])
    sym = one(app.conn, "SELECT symbol FROM security WHERE id=?", (e["security_id"],))["symbol"] if e["security_id"] else "portfolio"
    recs = [rec_mod.latest_for(app, pid, e["security_id"]) for pid in pids] if e["security_id"] else []
    recs = [r for r in recs if r]
    thesis = None
    if e["security_id"]:
        from ..research.thesis import current_version
        thesis = current_version(app, e["security_id"])
    why = ("Invalidation conditions on record: " + "; ".join(c["description"] for c in thesis.conditions)) if thesis else \
        "No approved thesis on record: the impact cannot be assessed against a thesis."
    body = [f"**{sym}** — {e['event_type']}: {p.get('label', '')}", "",
            f"- Why it may matter: {why}",
            f"- Evidence: {p.get('url') or p.get('recommendation_id') or 'see event ' + e['id']} (public {e['public_at']})",
            f"- Detected: {e['detected_at']} by scheduled polling (not real-time)"]
    for r in recs:
        body.append(f"- Current recommendation: **{r['action']}** (business {r['business_assessment']}) as of {r['as_of']}"
                    + (" — PREVIEW" if r["is_preview"] else ""))
        body.append(f"- Data freshness: price {r['payload']['freshness'].get('price_date')}, filings checked "
                    f"{r['payload']['freshness'].get('filings_checked_hours_ago')} h before the review")
    if e["event_type"] in ("ACTION_CHANGE", "ELIGIBILITY_CHANGE") and p.get("recommendation_id"):
        r = rec_mod.get(app, p["recommendation_id"])
        cc = r["payload"].get("current_conditions") or {}
        body.append(f"- Purchase eligibility: {r['purchase_eligibility']} (was {(p.get('eligibility') or {}).get('before')})")
        for pz in cc.get("pauses", []):
            srcs = ", ".join(f"{s_.get('observation') or s_.get('source_id')} ({s_.get('public_at')})"
                             for s_ in pz.get("sources", [])[:5]) or "no market observation (see detail)"
            body.append(f"  - PAUSED {pz['code']}: {pz['detail']}. Evidence: {srcs}. Reassess: {pz['reassess_condition']}"
                        + (f" (by {pz['reassess_on']})" if pz.get("reassess_on") else ""))
        for b_ in cc.get("blocks", []):
            body.append(f"  - BLOCKED: {b_}")
        if r["purchase_eligibility"] == "ELIGIBLE" and (p.get("eligibility") or {}).get("before") != "ELIGIBLE":
            body.append("  - The earlier pause/block no longer applies; the next allocation re-validates it at its cutoff.")
    body.append("- Required next step: " + ("review the filing against the thesis and record your assessment "
                                              "(`eqm thesis assess`), then re-run `eqm review`." if e["event_type"] == "NEW_FILING"
                                              else "read the recommendation and record a decision (`eqm decide`)."))
    title = f"{sym}: {p.get('label', e['event_type'])}"
    # Cooldown suppresses repeats of the same development; a decision/eligibility change is keyed on the exact
    # transition, so a reversal (e.g. PAUSED -> ELIGIBLE soon after ELIGIBLE -> PAUSED) is never suppressed.
    group = title if e["event_type"] in ("ACTION_CHANGE", "ELIGIBILITY_CHANGE") else f"{sym}: "
    create_alert(app, f"alert:{e['event_key']}", "MATERIAL_EVENT", title,
                 "\n".join(body), severity=e["severity"], event_id=e["id"], security_id=e["security_id"],
                 cooldown_group=group)


def weekly_digest(app: App, run_id: str, scheduled_for: datetime, ctx: JobContext | None = None) -> dict:
    ctx = ctx or JobContext()
    out = {}
    for p in _portfolios(app, ctx):
        md = reports.weekly_digest_md(app, p, app.now())
        name = one(app.conn, "SELECT name FROM portfolio WHERE id=?", (p,))["name"]
        path, _ = reports.write_report(app, f"weekly_digest_{name}", md)
        wk = scheduled_for.isocalendar()
        create_alert(app, f"digest:{p}:{wk.year}-W{wk.week:02d}", "WEEKLY_DIGEST", f"Weekly digest — {name}", md,
                     severity="INFO")
        out[name] = str(path)
    return {"reports": out, "delivery": deliver_pending(app)}


def monthly_allocation(app: App, run_id: str, scheduled_for: datetime, ctx: JobContext | None = None) -> dict:
    ctx = ctx or JobContext()
    out = {}
    pids = _portfolios(app, ctx)
    for p in _research_portfolios(app, pids):
        before = {r["id"] for r in all_rows(app.conn, "SELECT id FROM recommendation WHERE portfolio_id=?", (p,))}
        prop = propose(app, p, as_of=app.now())
        # allocation re-validates every candidate; a changed decision it records is alerted like a daily change
        for eid in decision_change_events(app, p, [v["recommendation_id"] for v in prop.validated], before, run_id):
            _alert_for_event(app, eid, pids)
        from ..decisions.allocation import load
        md = reports.allocation_md(app, load(app, prop.id))
        name = one(app.conn, "SELECT name FROM portfolio WHERE id=?", (p,))["name"]
        path, _ = reports.write_report(app, f"allocation_{name}", md)
        planned = app.settings.contribution.monthly_amount_usd
        intro = (f"Planned monthly contribution: {planned} (not cash until you record the DEPOSIT). "
                 if planned else "No planned contribution configured. ")
        create_alert(app, f"alloc:{p}:{scheduled_for:%Y-%m}", "MONTHLY_ALLOCATION", f"Monthly allocation — {name}",
                     intro + "Confirm the deposit, then review the proposal.\n\n" + md, severity="MATERIAL")
        out[name] = {"proposal": prop.id, "report": str(path)}
    return {"proposals": out, "delivery": deliver_pending(app)}


def weekly_finder(app: App, run_id: str, scheduled_for: datetime, ctx: JobContext | None = None) -> dict:
    if not app.settings.finder_enabled:
        return {"skipped": "company finder is opt-in: set finder_enabled: true in config/user.yaml"}
    from ..research.finder import run_finder
    from ..research.finder_judge import JudgingRefused, export_pack, judge_run
    from ..llm.service import provider_from_settings
    rid = run_finder(app)
    detail: dict = {"finder_run": rid}
    judged = False
    if app.settings.finder_auto_judge and app.settings.llm.provider in ("claude_code", "anthropic"):
        try:
            detail["judgment"] = judge_run(app, provider_from_settings(app), rid)
            judged = True
        except JudgingRefused as exc:            # unattended CLI judging stays off until validated
            detail["judgment_refused"] = str(exc)
    if not judged:
        detail["pack"] = {k: str(v) for k, v in export_pack(app, rid, app.reports_dir / "finder" / rid).items()}
    md = reports.finder_md(app, rid)
    path, _ = reports.write_report(app, "finder_shortlist", md)
    n = app.conn.execute("SELECT shortlist_count FROM finder_run WHERE id=?", (rid,)).fetchone()[0]
    create_alert(app, f"finder:{rid}", "FINDER", f"Company finder: {n} research candidates", md, severity="INFO")
    detail["report"] = str(path)
    return detail


def handlers(ctx: JobContext | None = None) -> dict:
    return {
        "weekly_finder": lambda app, rid, when: weekly_finder(app, rid, when, ctx),
        "daily_refresh": lambda app, rid, when: daily_refresh(app, rid, when, ctx),
        "weekly_digest": lambda app, rid, when: weekly_digest(app, rid, when, ctx),
        "monthly_allocation": lambda app, rid, when: monthly_allocation(app, rid, when, ctx),
    }
