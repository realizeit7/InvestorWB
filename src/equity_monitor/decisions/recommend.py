"""Assemble point-in-time decision inputs, run the engine, persist recommendations and owner decisions.

Recommendations are immutable and never touch the ledger. Owner approvals/overrides are separate
``user_decision`` rows.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..app import App
from ..data import calendar as cal
from ..data.prices import price_on_or_before
from ..data.rawstore import open_quality_issues
from ..data.securities import security_ref
from ..db.core import all_rows, insert, one
from ..ledger.store import accounts_of, open_issues, portfolio_kind
from ..ledger.views import PortfolioView, portfolio_view
from ..research.conditions import evaluate_condition, evaluate_milestone
from ..research.fundamentals import FactView
from ..research.screening import ScreenInput, exclusion
from ..research.thesis import current_version, latest_assessment, record_assessment
from ..util import from_json, iso_utc, new_id, parse_utc, stable_hash, to_json
from ..valuation.store import latest_valuation
from ..market.exposures import current_profile
from ..market.impacts import analyze
from ..market.snapshot import snapshot_as_of
from .conditions import assess as assess_conditions, baseline as baseline_eligibility
from .engine import ConditionState, Decision, DecisionInputs, MilestoneState, decide

UNSUPPORTED = {"BANK_OR_CREDIT", "INSURANCE", "REIT", "SHELL", "PRE_REVENUE_BIOTECH", "UNKNOWN_INDUSTRY", "ETF_OR_FUND"}


def is_preview(app: App, portfolio_id: str) -> bool:
    return (portfolio_kind(app, portfolio_id) != "ACTUAL" or app.policy.status == "PREVIEW"
            or not app.settings.risk.confirmed)


def _conditions(app: App, fv: FactView, thesis, as_of: datetime) -> list[ConditionState]:
    out = []
    for c in thesis.conditions:
        if c["kind"] == "METRIC":
            state, ev = evaluate_condition(fv, c)
            prev = latest_assessment(app, "INVALIDATION", c["id"])
            if prev is None or prev["state"] != state or prev["assessor"] != "ENGINE":
                record_assessment(app, "INVALIDATION", c["id"], state, verified=state in ("TRIGGERED", "NOT_TRIGGERED"),
                                  assessor="ENGINE", evidence=ev, as_of=as_of)
            out.append(ConditionState(c["id"], c["description"], c["kind"], state, state == "TRIGGERED", "ENGINE",
                                      bool(ev.get("latest_breaches"))))
        else:
            a = latest_assessment(app, "INVALIDATION", c["id"], iso_utc(as_of))
            if a is None:
                out.append(ConditionState(c["id"], c["description"], c["kind"], "NOT_TRIGGERED", False, "NONE"))
            elif a["assessor"] == "LLM" and a["state"] == "TRIGGERED":
                out.append(ConditionState(c["id"], c["description"], c["kind"], "AMBIGUOUS", False, "LLM"))
            else:
                out.append(ConditionState(c["id"], c["description"], c["kind"], a["state"], bool(a["verified"]), a["assessor"]))
    return out


def gather_inputs(app: App, portfolio_id: str, security_id: str, as_of: datetime,
                  view: PortfolioView | None = None) -> tuple[DecisionInputs, dict]:
    ref = security_ref(app.conn, security_id)
    session = cal.latest_completed_session(as_of)
    view = view or portfolio_view(app, portfolio_id, session)
    fv = FactView(app, ref.issuer_id, as_of) if ref.issuer_id else None
    iss = one(app.conn, "SELECT * FROM issuer WHERE id=?", (ref.issuer_id,)) if ref.issuer_id else None
    ex = exclusion(app, ScreenInput(security_id, ref.symbol, iss["sic"] if iss else None, None, fv, None,
                                    ref.security_type)) if fv else "ETF_OR_FUND"
    supported = ex not in UNSUPPORTED and ref.security_type in ("COMMON", "ADR")
    unsupported_reason = ex if ex in UNSUPPORTED else (None if supported else f"security type {ref.security_type}")

    px = price_on_or_before(app, security_id, session)
    price, pdate = (px[1], px[0]) if px else (None, None)
    stale = cal.sessions_elapsed(pdate, session) if pdate else None

    chk = one(app.conn, "SELECT * FROM source_check WHERE subject=? AND check_type='FILINGS' AND checked_at<=? "
                        "ORDER BY checked_at DESC LIMIT 1", (ref.issuer_id, iso_utc(as_of))) if ref.issuer_id else None
    hours = (as_of - parse_utc(chk["checked_at"])).total_seconds() / 3600 if chk else None
    latest_pe = fv.latest_period_end() if fv else None
    overdue = latest_pe is None or (as_of.date() - latest_pe).days > app.policy.recommendation.max_financials_age_days

    dq = [f"{i['issue_code']}: {i['detail']}" for i in open_quality_issues(app)
          if i["severity"] == "CRITICAL" and i["ref_id"] in (security_id, ref.issuer_id)]
    acct_ids = [a["id"] for a in accounts_of(app, portfolio_id)]
    recon = [f"{i['issue_type']} ({i['id']})" for i in open_issues(app, acct_ids, security_id) if i["severity"] == "CRITICAL"]

    thesis = current_version(app, security_id, iso_utc(as_of))
    conds = _conditions(app, fv, thesis, as_of) if (thesis and fv) else []
    ms = []
    if thesis and fv:
        for m in thesis.milestones:
            st, _ = evaluate_milestone(fv, m, as_of.date())
            ms.append(MilestoneState(m["id"], m["description"], st))

    val = latest_valuation(app, security_id, iso_utc(as_of))
    new_fin = False
    if val and ref.issuer_id:
        new_fin = one(app.conn, "SELECT 1 FROM financial_fact WHERE issuer_id=? AND public_at>? AND public_at<=? "
                                "AND form IN ('10-K','10-Q','10-K/A','10-Q/A') LIMIT 1",
                      (ref.issuer_id, val.evidence_as_of, iso_utc(as_of))) is not None
    vflags = val.inputs.get("base", {}).get("review_flags", []) if val else []

    h = view.holding(security_id)
    iw = view.issuer_weights.get(ref.issuer_id or security_id) if view.nav else None
    sector = ref.sector or "Unknown"
    sw = view.sector_weights.get(sector, Decimal(0)) if view.nav else None

    # critical events not yet reviewed (after the latest approval/decision touching this security)
    last_review = max(filter(None, [
        thesis.approved_at if thesis else None,
        one(app.conn, "SELECT MAX(a.approved_at) AS t FROM exposure_approval a JOIN exposure_profile_version v "
                      "ON v.id=a.exposure_version_id WHERE v.security_id=?", (security_id,))["t"],
        one(app.conn, "SELECT MAX(a.approved_at) AS t FROM valuation_approval a JOIN valuation_version v "
                      "ON v.id=a.valuation_version_id WHERE v.security_id=?", (security_id,))["t"],
        one(app.conn, "SELECT MAX(d.decided_at) AS t FROM user_decision d JOIN recommendation r ON r.id=d.subject_id "
                      "WHERE d.subject_type='RECOMMENDATION' AND r.security_id=?", (security_id,))["t"],
    ]), default="")
    crit = [f"{e['event_type']}: {from_json(e['payload_json']).get('label', '')} ({e['public_at']})"
            for e in all_rows(app.conn, "SELECT * FROM detected_event WHERE severity='CRITICAL' AND verified=1 AND "
                                        "(security_id=? OR issuer_id=?) AND public_at<=? AND public_at>? ORDER BY public_at",
                              (security_id, ref.issuer_id, iso_utc(as_of), last_review))]

    prev = one(app.conn, "SELECT * FROM recommendation WHERE portfolio_id=? AND security_id=? AND as_of<? "
                         "ORDER BY as_of DESC, created_at DESC, rowid DESC LIMIT 1", (portfolio_id, security_id, iso_utc(as_of) + "~"))
    inp = DecisionInputs(
        security_id=security_id, symbol=ref.symbol, held=h is not None and h.shares > 0, supported=supported,
        unsupported_reason=unsupported_reason, price=price, price_date=pdate, price_stale_sessions=stale,
        filings_checked_hours_ago=hours, filings_check_failed=bool(chk and not chk["success"]),
        financials_overdue=overdue, latest_period_end=latest_pe, critical_data_issues=dq, reconciliation_issues=recon,
        thesis_version_id=thesis.id if thesis else None,
        thesis_next_review=date.fromisoformat(thesis.content["next_review_date"]) if thesis else None,
        conditions=conds, milestones=ms, valuation_id=val.id if val else None,
        valuation_approved=bool(val and val.approved), downside_reviewed=bool(val and val.downside_reviewed),
        valuation_age_days=(as_of - parse_utc(val.created_at)).days if val else None, valuation_review_flags=vflags,
        new_financials_since_valuation=new_fin, bear=val.bear if val else None, base=val.base if val else None,
        bull=val.bull if val else None, base_meaningful=bool(val and val.base_meaningful), position_weight=iw,
        sector=sector, sector_weight=sw, nav=view.nav, position_value=h.market_value if h else None,
        unreviewed_critical_events=crit, previous_action=prev["action"] if prev else None,
    )
    ctx = {"prev": dict(prev) if prev else None, "valuation": val, "thesis": thesis, "session": session,
           "filings_check": dict(chk) if chk else None, "view": view, "last_review": last_review, "ref": ref}
    return inp, ctx


def _changes(app: App, inp: DecisionInputs, d: Decision, ctx: dict, security_id: str, as_of: datetime) -> dict:
    prev = ctx["prev"]
    if prev is None:
        return {"first_review": True}
    pp = from_json(prev["payload_json"])
    ref = security_ref(app.conn, security_id)
    docs = [dict(r) for r in all_rows(app.conn, "SELECT id, doc_type, accession_no, public_at, items FROM source_document "
                                                "WHERE issuer_id=? AND public_at>? AND public_at<=? ORDER BY public_at",
                                      (ref.issuer_id, prev["as_of"], iso_utc(as_of)))] if ref.issuer_id else []
    return {
        "since": prev["as_of"], "previous_action": prev["action"], "action_changed": prev["action"] != d.action,
        "business_changed": prev["business_assessment"] != d.business,
        "price": {"before": pp.get("price"), "now": inp.price},
        "margin_of_safety": {"before": pp.get("margin_of_safety"), "now": d.margin_of_safety},
        "thesis_version_changed": prev["thesis_version_id"] != inp.thesis_version_id,
        "valuation_version_changed": prev["valuation_version_id"] != inp.valuation_id,
        "new_documents": docs,
        "reason_codes": {"before": from_json(prev["reason_codes_json"]), "now": d.reason_codes},
    }


def generate(app: App, portfolio_id: str, security_id: str, as_of: datetime | None = None,
             view: PortfolioView | None = None, force: bool = False) -> str:
    """Create a recommendation (or return the previous id if nothing in the inputs changed)."""
    as_of = as_of or app.now()
    inp, ctx = gather_inputs(app, portfolio_id, security_id, as_of, view)
    d = decide(inp, app.policy.recommendation, app.policy.portfolio)
    ref = ctx["ref"]
    snapshot = snapshot_as_of(app, as_of)
    profile = current_profile(app, security_id, iso_utc(as_of))
    impacts = analyze(app, security_id=security_id, issuer_id=ref.issuer_id, symbol=ref.symbol, sector=inp.sector,
                      as_of=as_of, snapshot=snapshot, profile=profile, last_review_at=ctx["last_review"],
                      valuation=ctx["valuation"])
    cash_unrec = any(i["issue_type"] == "NEGATIVE_CASH" for i in ctx["view"].open_issues)
    cc = assess_conditions(inp, d, app.policy.portfolio, app.policy.market, impacts, has_profile=profile is not None,
                           cash_unreconciled=cash_unrec, today=as_of.date())
    base_elig = baseline_eligibility(inp, d, app.policy.portfolio, cash_unrec, as_of.date())
    hashed = {k: v for k, v in asdict(inp).items() if k != "previous_action"}
    ihash = stable_hash({"inputs": hashed, "policy": app.policy.content_hash(), "exposure": profile[0] if profile else None,
                         "conditions": impacts.decision_relevant(), "eligibility": cc.eligibility})
    prev = ctx["prev"]
    if prev and prev["input_hash"] == ihash and prev["action"] == d.action and not force:
        return prev["id"]   # nothing decision-relevant changed (context-only developments do not create rows)
    thesis = ctx["thesis"]
    next_review = min(filter(None, [
        inp.thesis_next_review,
        (inp.latest_period_end + timedelta(days=100)) if inp.latest_period_end else None,
        as_of.date() + timedelta(days=app.policy.recommendation.next_review_days),
    ]))
    evidence = []
    if thesis:
        for c in thesis.claims:
            evidence.append({"text": c["text"], "type": c["claim_type"], "verification": c["verification"],
                             "supports": bool(c["links"][0]["supports"]) if c["links"] else True,
                             "links": [{k: l[k] for k in ("document_id", "passage_id", "fact_id", "quote", "verified")}
                                       for l in c["links"]]})
    payload = {
        "symbol": inp.symbol, "price": inp.price, "price_date": inp.price_date, "margin_of_safety": d.margin_of_safety,
        "price_to_base": d.price_to_base, "changes": _changes(app, inp, d, ctx, security_id, as_of),
        "evidence": evidence,
        "freshness": {"price_date": inp.price_date, "price_stale_sessions": inp.price_stale_sessions,
                      "last_completed_session": ctx["session"], "filings_checked_hours_ago": inp.filings_checked_hours_ago,
                      "filings_check_failed": inp.filings_check_failed, "latest_period_end": inp.latest_period_end,
                      "financials_overdue": inp.financials_overdue,
                      "new_financials_since_valuation": inp.new_financials_since_valuation},
        "downside": d.downside, "concentration": d.concentration, "missing": d.missing, "urgent": d.urgent,
        "next_review": next_review, "change_conditions": d.change_conditions, "proposed_trade": d.proposed_trade,
        "conditions": [asdict(c) for c in inp.conditions], "milestones": [asdict(m) for m in inp.milestones],
        "held": inp.held,
        "long_term_case": _long_term_case(app, security_id, d, inp, ctx),
        "current_conditions": cc.to_dict(),
        "purchase_eligibility": cc.eligibility, "baseline_eligibility": base_elig,
        "market_context": {"snapshot_id": impacts.snapshot_id, "snapshot_as_of": snapshot["as_of"] if snapshot else None,
                           "snapshot_missing": snapshot["missing"] if snapshot else ["no market snapshot available"],
                           "developments": impacts.developments},
        "exposure_version_id": impacts.exposure_version_id,
        "chains": [asdict(c) for c in impacts.chains], "clusters": impacts.clusters,
        "valuation_changes": {"proposals": impacts.valuation_proposals, "stress_downside": impacts.stress_downside,
                              "note": "proposals are not applied; material changes need a new valuation version + approval"},
        "position_implications": _position_implications(app, d, inp, cc),
        "disclaimer": "Decision support only. No order has been placed; holdings are unchanged.",
    }
    payload["changes"]["eligibility"] = {"before": prev["purchase_eligibility"] if prev else None, "now": cc.eligibility,
                                         "pauses_now": [p.code for p in cc.pauses]}
    for u in impacts.unknowns:
        if u not in payload["missing"]:
            payload["missing"].append(u)
    kind = portfolio_kind(app, portfolio_id)
    rid = new_id("rec")
    insert(app.conn, "recommendation", {
        "id": rid, "portfolio_id": portfolio_id, "security_id": security_id, "as_of": iso_utc(as_of),
        "created_at": app.now_iso(), "business_assessment": d.business, "action": d.action,
        "previous_action": prev["action"] if prev else None, "previous_recommendation_id": prev["id"] if prev else None,
        "reason_codes_json": to_json(d.reason_codes), "explanation": d.explanation, "payload_json": to_json(payload),
        "thesis_version_id": inp.thesis_version_id, "valuation_version_id": inp.valuation_id,
        "policy_version_id": app.policy_version_id(), "input_hash": ihash, "is_preview": int(is_preview(app, portfolio_id)),
        "label": kind, "purchase_eligibility": cc.eligibility, "baseline_eligibility": base_elig,
        "market_snapshot_id": impacts.snapshot_id, "exposure_version_id": impacts.exposure_version_id,
    })
    _raise_research_tasks(app, security_id, rid, cc)
    app.audit("recommendation.created", "recommendation", rid, {"action": d.action, "security_id": security_id})
    return rid


def _long_term_case(app: App, security_id: str, d: Decision, inp: DecisionInputs, ctx: dict) -> dict:
    from ..research.thesis import original_version
    orig, cur = original_version(app, security_id), ctx["thesis"]
    val = ctx["valuation"]
    return {"business_assessment": d.business, "action": d.action,
            "original_thesis": {"version_id": orig.id, "version_no": orig.version_no, "approved_at": orig.approved_at} if orig else None,
            "current_thesis": {"version_id": cur.id, "version_no": cur.version_no, "approved_at": cur.approved_at,
                               "change_reason": cur.change_reason} if cur else None,
            "thesis_changed_since_original": bool(orig and cur and orig.id != cur.id),
            "valuation": {"version_id": val.id, "approved": val.approved, "bear": val.bear, "base": val.base, "bull": val.bull}
            if val else None,
            "margin_of_safety": d.margin_of_safety}


def _position_implications(app: App, d: Decision, inp: DecisionInputs, cc) -> dict:
    pp = app.policy.portfolio
    room = None
    if inp.nav and inp.position_weight is not None:
        room = max(Decimal(0), min(pp.target_position_weight, pp.max_issuer_weight) - inp.position_weight) * inp.nav
    if cc.eligibility == "BLOCKED":
        txt = "No new purchases: " + "; ".join(cc.blocks)
    elif d.action == "ADD" and cc.eligibility == "ELIGIBLE":
        txt = f"Eligible for the next monthly contribution, up to about {room:,.2f} of room to the target weight" if room is not None \
            else "Eligible for the next monthly contribution (room unknown: NAV incomplete)"
    elif d.action == "ADD":
        txt = "Long-term case supports adding, but purchases are PAUSED (" + ", ".join(p.code for p in cc.pauses) + \
              "); the next contribution skips it until the pause is reassessed"
    else:
        txt = f"{d.action}: keep the position; no new money preferred" + \
              (f"; purchases also PAUSED ({', '.join(p.code for p in cc.pauses)})" if cc.pauses else "")
    if d.proposed_trade:
        txt += f". Proposed (not executed): {d.proposed_trade.get('side')} ~{d.proposed_trade.get('amount')}"
    return {"summary": txt, "room_to_target": room, "limits_unchanged": "market context never relaxes cash, issuer or sector limits"}


def _raise_research_tasks(app: App, security_id: str, rec_id: str, cc) -> None:
    for r in cc.research:
        insert(app.conn, "research_task", {"id": new_id("rt"), "task_key": f"{security_id}:{r['cluster']}:{r['reason']}",
                                           "security_id": security_id, "reason": r["reason"],
                                           "detail_json": to_json({**r, "recommendation_id": rec_id}), "status": "OPEN",
                                           "created_at": app.now_iso(), "closed_at": None}, or_ignore=True)


def review_portfolio(app: App, portfolio_id: str, as_of: datetime | None = None, include_watchlist: bool = True) -> list[str]:
    """Generate recommendations for every non-ETF holding (+ approved watchlist). Returns ids."""
    as_of = as_of or app.now()
    view = portfolio_view(app, portfolio_id, cal.latest_completed_session(as_of))
    sids = [h.security_id for h in view.holdings if h.security_type not in ("ETF", "FUND")]
    if include_watchlist:
        sids += [r["security_id"] for r in all_rows(app.conn, "SELECT security_id FROM watchlist_entry WHERE status IN "
                                                              "('APPROVED','RESEARCH')") if r["security_id"] not in sids]
    return [generate(app, portfolio_id, sid, as_of, view) for sid in sids]


def get(app: App, rec_id: str) -> dict:
    r = dict(one(app.conn, "SELECT * FROM recommendation WHERE id=?", (rec_id,)))
    r["payload"] = from_json(r.pop("payload_json"))
    r["reason_codes"] = from_json(r.pop("reason_codes_json"))
    return r


def latest_for(app: App, portfolio_id: str, security_id: str) -> dict | None:
    r = one(app.conn, "SELECT id FROM recommendation WHERE portfolio_id=? AND security_id=? ORDER BY as_of DESC, "
                      "created_at DESC, rowid DESC LIMIT 1", (portfolio_id, security_id))
    return get(app, r["id"]) if r else None


def history_for(app: App, portfolio_id: str, security_id: str) -> list[dict]:
    return [get(app, r["id"]) for r in all_rows(app.conn, "SELECT id FROM recommendation WHERE portfolio_id=? AND "
                                                          "security_id=? ORDER BY as_of, created_at, rowid", (portfolio_id, security_id))]


# ------------------------------------------------------------------ watchlist & owner decisions
def set_watchlist(app: App, security_id: str, status: str, note: str | None = None) -> None:
    now = app.now_iso()
    if one(app.conn, "SELECT 1 FROM watchlist_entry WHERE security_id=?", (security_id,)):
        app.conn.execute("UPDATE watchlist_entry SET status=?, updated_at=?, note=COALESCE(?, note) WHERE security_id=?",
                         (status, now, note, security_id))
    else:
        insert(app.conn, "watchlist_entry", {"security_id": security_id, "status": status, "added_at": now,
                                             "updated_at": now, "note": note})
    app.audit("watchlist.set", "security", security_id, {"status": status})


def record_decision(app: App, subject_type: str, subject_id: str, decision: str, *, rationale: str = "",
                    override_action: str | None = None, decided_by: str = "owner") -> str:
    if decision == "OVERRIDE" and not override_action:
        raise ValueError("OVERRIDE requires override_action")
    if decision == "OVERRIDE" and not rationale.strip():
        raise ValueError("an override requires a written rationale")
    did = new_id("dec")
    insert(app.conn, "user_decision", {"id": did, "subject_type": subject_type, "subject_id": subject_id,
                                       "decision": decision, "override_action": override_action,
                                       "rationale": rationale, "decided_at": app.now_iso(), "decided_by": decided_by})
    app.audit("decision.recorded", subject_type.lower(), subject_id, {"decision": decision, "override": override_action})
    return did


def decisions_for(app: App, subject_id: str) -> list[dict]:
    return [dict(r) for r in all_rows(app.conn, "SELECT * FROM user_decision WHERE subject_id=? ORDER BY decided_at",
                                      (subject_id,))]
