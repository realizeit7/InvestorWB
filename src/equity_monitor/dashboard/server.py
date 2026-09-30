"""Local dashboard (read-mostly). Binds to 127.0.0.1 only.

Views: Portfolio, Company, Monthly allocation, System health, Inbox. The only writes are owner
actions (acknowledge/snooze alerts, record a decision), protected by a per-process CSRF token.
Nothing here can place trades or modify the ledger.
"""

from __future__ import annotations

import html
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..app import open_app
from ..data import calendar as cal
from ..decisions import recommend as rec_mod
from ..decisions.allocation import latest as latest_alloc
from ..ledger.views import portfolio_view
from ..monitoring.alerts import acknowledge, inbox, snooze
from ..monitoring.health import system_health
from ..reporting.reports import banner
from ..research.thesis import current_version, history, original_version
from ..util import fmt_money, fmt_pct, from_json

TEMPLATES = Path(__file__).parent / "templates"
CSRF = secrets.token_urlsafe(24)


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters["money"] = fmt_money
    env.filters["pct"] = fmt_pct
    env.filters["num"] = lambda x: "n/a" if x is None else f"{float(x):,.2f}"
    return env


def _banner_html(app, pid) -> list[str]:
    return [l.lstrip("> ").replace("**", "") for l in banner(app, pid).splitlines()]


def make_wsgi(args):
    env = _env()

    def app_factory():
        return open_app(args.home, policy_path=args.policy, settings_path=args.settings)

    def respond(start, body: str, status="200 OK", ctype="text/html; charset=utf-8"):
        start(status, [("Content-Type", ctype), ("X-Frame-Options", "DENY"), ("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")])
        return [body.encode("utf-8")]

    def wsgi(environ, start):
        app = app_factory()
        try:
            path = environ.get("PATH_INFO", "/")
            qs = parse_qs(environ.get("QUERY_STRING", ""))
            portfolios = [dict(r) for r in app.conn.execute("SELECT * FROM portfolio ORDER BY created_at")]
            if not portfolios:
                return respond(start, env.get_template("empty.html").render(title="Equity monitor"))
            pname = qs.get("portfolio", [app.settings.active_portfolio or portfolios[0]["name"]])[0]
            pf = next((p for p in portfolios if p["name"] == pname or p["id"] == pname), portfolios[0])
            ctx = {"portfolios": portfolios, "pf": pf, "banner": _banner_html(app, pf["id"]), "csrf": CSRF, "path": path}
            if environ["REQUEST_METHOD"] == "POST":
                size = int(environ.get("CONTENT_LENGTH") or 0)
                form = {k: v[0] for k, v in parse_qs(environ["wsgi.input"].read(size).decode()).items()}
                if form.get("csrf") != CSRF:
                    return respond(start, "invalid CSRF token", "403 Forbidden", "text/plain")
                if path == "/alerts/ack":
                    acknowledge(app, form["alert_id"], form.get("note"))
                elif path == "/alerts/snooze":
                    snooze(app, form["alert_id"], app.now() + timedelta(days=int(form.get("days", "7"))), form.get("note"))
                elif path == "/decide":
                    rec_mod.record_decision(app, form["subject_type"], form["subject_id"], form["decision"],
                                            rationale=form.get("rationale", ""), override_action=form.get("override_action") or None)
                start("303 See Other", [("Location", form.get("back", "/") )])
                return [b""]
            if path == "/":
                view = portfolio_view(app, pf["id"], cal.latest_completed_session(app.now()))
                rows = []
                for h in view.holdings:
                    r = rec_mod.latest_for(app, pf["id"], h.security_id) if h.security_type not in ("ETF", "FUND") else None
                    rows.append({"h": h, "r": r})
                wl = [dict(w) | {"r": rec_mod.latest_for(app, pf["id"], w["security_id"])} for w in app.conn.execute(
                    "SELECT w.*, s.symbol FROM watchlist_entry w JOIN security s ON s.id=w.security_id WHERE w.status IN ('APPROVED','RESEARCH')")]
                return respond(start, env.get_template("portfolio.html").render(title="Portfolio", view=view, rows=rows, wl=wl, **ctx))
            if path.startswith("/company/"):
                sid = path.split("/")[2]
                sec = app.conn.execute("SELECT s.*, i.name AS issuer_name, i.sector, i.cik FROM security s LEFT JOIN issuer i "
                                       "ON i.id=s.issuer_id WHERE s.id=?", (sid,)).fetchone()
                vals = [dict(v) | {"out": from_json(v["outputs_json"])} for v in app.conn.execute(
                    "SELECT v.*, a.approved_at, a.downside_reviewed FROM valuation_version v LEFT JOIN valuation_approval a "
                    "ON a.valuation_version_id=v.id WHERE v.security_id=? ORDER BY version_no", (sid,))]
                recs = rec_mod.history_for(app, pf["id"], sid)
                for r in recs:
                    r["decisions"] = rec_mod.decisions_for(app, r["id"])
                from ..reporting.reports import holding_section_md
                from ..reporting.markdown import md_fragment
                from ..market.exposures import profile_history
                review_html = md_fragment("\n".join(holding_section_md(recs[-1]))) if recs else ""
                exposures = [dict(v) | {"content": from_json(v["content_json"]), "verification": from_json(v["verification_json"])}
                             for v in profile_history(app, sid)]
                return respond(start, env.get_template("company.html").render(
                    title=sec["symbol"], sec=sec, original=original_version(app, sid), current=current_version(app, sid),
                    history=history(app, sid), vals=vals, recs=recs, review_html=review_html, exposures=exposures, **ctx))
            if path == "/allocation":
                a = latest_alloc(app, pf["id"])
                decs = rec_mod.decisions_for(app, a["id"]) if a else []
                return respond(start, env.get_template("allocation.html").render(title="Allocation", a=a, p=a["payload"] if a else None,
                                                                                 decisions=decs, **ctx))
            if path == "/market":
                from ..market.snapshot import snapshot_as_of
                from ..reporting.reports import market_context_md
                from ..reporting.markdown import md_fragment
                from ..market.lookthrough import portfolio_market_exposure
                from ..market.snapshot import load_snapshot
                latest = app.conn.execute("SELECT id FROM market_snapshot ORDER BY as_of DESC, rowid DESC LIMIT 1").fetchone()
                snap = load_snapshot(app, latest["id"]) if latest else None   # display: latest snapshot, with its own timestamp
                body = md_fragment(market_context_md(app, snap)) if snap else "<p>No market snapshot yet: run <code>eqm market refresh</code>.</p>"
                try:
                    lt = portfolio_market_exposure(app, pf["id"], cal.latest_completed_session(app.now()))
                except Exception:  # noqa: BLE001
                    lt = None
                return respond(start, env.get_template("market.html").render(title="Market context", body=body, lt=lt, **ctx))
            if path == "/health":
                return respond(start, env.get_template("health.html").render(title="System health", h=system_health(app), **ctx))
            if path == "/inbox":
                return respond(start, env.get_template("inbox.html").render(title="Inbox", alerts=inbox(app, include_acknowledged="all" in qs),
                                                                            **ctx))
            return respond(start, "not found", "404 Not Found", "text/plain")
        except Exception as exc:  # noqa: BLE001 - show errors locally instead of a blank page
            return respond(start, f"<pre>{html.escape(type(exc).__name__ + ': ' + str(exc))}</pre>", "500 Internal Server Error")
        finally:
            app.conn.close()

    return wsgi


def run(args, port: int = 8765) -> None:  # pragma: no cover - interactive
    with make_server("127.0.0.1", port, make_wsgi(args)) as srv:
        print(f"dashboard on http://127.0.0.1:{port}/ (local only; Ctrl-C to stop)")
        srv.serve_forever()
