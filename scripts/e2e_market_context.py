"""Live market-context example on REAL data (ILLUSTRATIVE; run after scripts/e2e_real_company.py).

    EQM_SEC_USER_AGENT="Your Name you@example.com" uv run python scripts/e2e_market_context.py AAPL

Refreshes reference ETFs/indices/futures (Yahoo), FRED series, FINRA short interest and short-sale volume,
builds the shared snapshot, drafts + approves an ILLUSTRATIVE exposure profile in the separate var/e2e home
(never your real data), and prints the company's current-conditions chain. Nothing here is advice.
"""

from __future__ import annotations

import os
import sys

from equity_monitor.app import open_app
from equity_monitor.config.models import UserSettings
from equity_monitor.data.prices import provider_from_settings
from equity_monitor.data.securities import resolve
from equity_monitor.decisions.recommend import generate, get
from equity_monitor.market.exposures import approve_profile, create_profile, draft_default_profile
from equity_monitor.market.snapshot import load_snapshot
from equity_monitor.monitoring.jobs import JobContext, refresh_market_context
from equity_monitor.reporting import reports


def main(symbol: str) -> None:
    app = open_app("var/e2e", settings_path="none", policy_path="none")
    app.settings = UserSettings(sec_user_agent=os.environ.get("EQM_SEC_USER_AGENT"))
    sid = resolve(app.conn, symbol)
    prof = draft_default_profile(app, sid)
    vid = create_profile(app, sid, prof, change_reason="e2e illustrative draft", author="ENGINE", label="ILLUSTRATIVE")
    approve_profile(app, vid, approver="e2e-script", note="ILLUSTRATIVE approval in the example home only")
    out = refresh_market_context(app, JobContext(), [sid], provider_from_settings(app), None)
    snap = load_snapshot(app, out["snapshot_id"])
    print("market refresh:", {k: v for k, v in out.items() if k != "short_interest"}, "short interest ok:", out.get("short_interest"))
    print("flags:", [f["flag"] for f in snap["flags"]], "missing:", snap["missing"], "stale:", snap["stale"])
    for k, v in snap["indicators"].items():
        print(f"  {k:<22} {v['status']:<7} {v.get('value')!s:>10} period {v.get('period')} public {v.get('public_at')} {v.get('vintage_basis')}")
    for s in ("SPY", "QQQ", "^VIX"):
        m = snap["instruments"][s]
        print(f"  {s:<5} 1m {m.get('ret_1m')} 3m {m.get('ret_3m')} dd {m.get('drawdown_from_52w_high')} close {m.get('last_close')}")
    pf = app.conn.execute("SELECT id FROM portfolio WHERE name='e2e-illustrative'").fetchone()["id"]
    r = get(app, generate(app, pf, sid, force=True))
    print(f"{symbol}: action {r['action']} / purchases {r['purchase_eligibility']} (baseline {r['baseline_eligibility']})")
    print("\n".join(reports.holding_section_md(r)))
    reports.write_report(app, "market_context", reports.market_context_md(app, snap), out_dir=__import__("pathlib").Path("reports/equity/examples"))


if __name__ == "__main__":
    main((sys.argv[1:] or ["AAPL"])[0].upper())
