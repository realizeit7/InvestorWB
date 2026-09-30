"""End-to-end example on REAL public data (SEC EDGAR + market prices), labelled ILLUSTRATIVE.

Usage:
    EQM_SEC_USER_AGENT="Your Name you@example.com" uv run python scripts/e2e_real_company.py AAPL MSFT KO PEP CAT

What it does (in a separate data home, var/e2e, in a HYPOTHETICAL portfolio so it can never mix with
your actual holdings):
  1. registers the tickers from the SEC ticker map, syncs filings and XBRL facts;
  2. fetches ~1y of daily prices (market_data_provider from settings, default yahoo_chart);
  3. screens them (research shortlist only);
  4. builds bear/base/bull DCFs for the first ticker (not approved) + reverse DCF;
  5. fetches the latest 10-K text, writes a thesis whose FACT claims cite real passages, and
     shows the verification result (no LLM needed);
  6. generates the recommendation (expected: REVIEW, because nothing was approved by the owner);
  7. writes company/portfolio reports to reports/equity/examples/<date>/.
Nothing is approved on the owner's behalf; outputs are illustrative, not advice.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from equity_monitor.app import open_app
from equity_monitor.config.models import UserSettings
from equity_monitor.data import calendar as cal
from equity_monitor.data import sec
from equity_monitor.data.prices import price_on_or_before, provider_from_settings, refresh_prices
from equity_monitor.decisions.recommend import generate, get, set_watchlist
from equity_monitor.ledger.store import create_account, create_portfolio
from equity_monitor.reporting import reports
from equity_monitor.research.evidence import Citation, ClaimIn
from equity_monitor.research.fundamentals import FactView, ingest_companyfacts
from equity_monitor.research.screening import run_screen
from equity_monitor.research.thesis import ConditionIn, ThesisContent, create_version, history
from equity_monitor.valuation.builder import build_scenarios
from equity_monitor.valuation.dcf import ScenarioInputs, margin_of_safety, reverse_dcf, run_dcf
from equity_monitor.valuation.store import create_valuation, latest_valuation


def find_passage(app, doc_id: str, pattern: str):
    rx = re.compile(pattern, re.I)
    for r in app.conn.execute("SELECT id, text FROM document_passage WHERE document_id=? ORDER BY ordinal", (doc_id,)):
        m = rx.search(r["text"])
        if m:
            s = max(0, m.start() - 10)
            quote = r["text"][m.start():min(len(r["text"]), m.end() + 140)].split("\n")[0]
            return r["id"], quote
    return None, None


def main(tickers: list[str]) -> None:
    ua = os.environ.get("EQM_SEC_USER_AGENT")
    if not ua:
        sys.exit("set EQM_SEC_USER_AGENT='Your Name you@example.com' (SEC fair-access policy)")
    app = open_app("var/e2e", settings_path="none", policy_path="none")
    app.settings = UserSettings(sec_user_agent=ua)
    client = sec.make_client(app)
    pf = create_portfolio(app, "e2e-illustrative", "HYPOTHETICAL", note="Illustrative real-data example; not holdings")
    create_account(app, pf, "none")
    sids = {}
    for t in tickers:
        sid = sec.register_from_ticker(app, client, t)
        iss = app.conn.execute("SELECT issuer_id FROM security WHERE id=?", (sid,)).fetchone()[0]
        res = sec.sync_filings(app, client, iss)
        raw, rid = sec.fetch_companyfacts(app, client, iss)
        stats = ingest_companyfacts(app, iss, raw, rid)
        sids[t] = (sid, iss)
        print(f"{t}: {len(res.new_document_ids)} filings indexed, facts {stats}")
    pr = refresh_prices(app, [s for s, _ in sids.values()], provider_from_settings(app), lookback_days=400)
    for t, (s, _) in sids.items():
        print(f"{t}: prices {pr[s]}")
    run_id, rows = run_screen(app, [s for s, _ in sids.values()], app.now(), label="CURRENT")
    print("screen (research shortlist, not a recommendation):")
    for r in sorted(rows, key=lambda r: (r.rank or 99)):
        print(f"  {r.symbol:<6} rank {r.rank} score {r.score and round(float(r.score), 3)} excluded={r.exclusion_reason}")

    t0 = tickers[0]
    sid, iss = sids[t0]
    fv = FactView(app, iss, app.now())
    scen = build_scenarios(fv, app.policy.valuation)
    vid = create_valuation(app, sid, scen, evidence_as_of=app.now(), author="ENGINE", label="ILLUSTRATIVE",
                           change_reason="e2e example (not owner-approved)")
    v = latest_valuation(app, sid)
    px = price_on_or_before(app, sid, cal.latest_completed_session(app.now()))
    print(f"{t0} DCF per share bear/base/bull: {v.bear:.2f} / {v.base:.2f} / {v.bull:.2f}; price {px}")
    if px:
        print(f"  margin of safety vs base: {margin_of_safety(px[1], v.base, v.base_meaningful)}")
        for var in ("revenue_growth", "ebit_margin", "wacc"):
            r = reverse_dcf(ScenarioInputs.model_validate(v.inputs["base"]), px[1], var, cap=app.policy.valuation.terminal_growth_cap)
            print(f"  reverse DCF {var}: {r.status} implied={r.implied_value and round(float(r.implied_value), 4)}")

    doc = app.conn.execute("SELECT id FROM source_document WHERE issuer_id=? AND doc_type='10-K' ORDER BY public_at DESC LIMIT 1",
                           (iss,)).fetchone()
    n = sec.fetch_document_text(app, client, doc["id"])
    print(f"{t0} latest 10-K text: {n} passages")
    claims = []
    for pattern, text in ((r"net sales", "The 10-K discusses net sales"),
                          (r"competition", "The 10-K describes competitive pressure")):
        pid, quote = find_passage(app, doc["id"], pattern)
        if pid:
            claims.append(ClaimIn(text=text, claim_type="FACT", citations=[Citation(passage_id=pid, quote=quote)]))
    rev = fv.annual("revenue")[-1]
    claims.append(ClaimIn(text=f"Fiscal-year revenue was {rev.value:,.0f} USD", claim_type="FACT",
                          citations=[Citation(fact_id=rev.fact_id)]))
    claims.append(ClaimIn(text="Fabricated: revenue tripled", claim_type="FACT",
                          citations=[Citation(passage_id=f"{doc['id']}#p99999", quote="revenue tripled")]))
    content = ThesisContent(
        business_model="ILLUSTRATIVE placeholder written by the example script, not an investment view.",
        valuation_requires="See reverse DCF output.", our_view_differs="None stated (illustrative).",
        evidence=claims, counterargument="Not assessed (illustrative).",
        invalidation_conditions=[ConditionIn(description="Operating margin below 15% for 2 consecutive FYs", kind="METRIC",
                                             concept="operating_income", metric="margin", comparator="<",
                                             threshold=Decimal("0.15"), consecutive_periods=2)],
        value_realization="n/a", key_risks=["illustrative"], assumptions=["illustrative"], next_review_date=date(2027, 3, 31))
    tv = create_version(app, sid, content, change_reason="e2e example", author="USER", label="ILLUSTRATIVE")
    for c in [h for h in history(app, sid) if h.id == tv][0].claims:
        print(f"  claim [{c['verification']}] {c['text'][:70]} {c['verification_detail'] or ''}")
    set_watchlist(app, sid, "RESEARCH", "e2e illustrative")
    rec = get(app, generate(app, pf, sid))
    print(f"{t0} recommendation: {rec['action']} ({', '.join(rec['reason_codes'])})")
    out = Path("reports/equity/examples")
    reports.write_report(app, f"company_{t0}", reports.company_md(app, pf, sid), out_dir=out)
    reports.write_report(app, "e2e_portfolio_review", reports.portfolio_review_md(app, pf), out_dir=out)
    print(f"reports written under {out}/{app.now().date()}/")


if __name__ == "__main__":
    main([t.upper() for t in (sys.argv[1:] or ["AAPL", "MSFT", "KO", "PEP", "CAT", "JPM"])])
