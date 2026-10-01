"""Pre-pilot setup check: what is configured, what the owner still has to supply, which integrations are
untested. Secrets are reported only as present/absent — values are never printed."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from .app import App
from .db.core import all_rows, one

OK, MISSING, WARN, INFO = "OK", "MISSING", "WARN", "INFO"


def _present(var: str) -> bool:
    return bool(os.environ.get(var))


def setup_check(app: App, settings_path: str | Path | None = None) -> dict:
    s = app.settings
    items: list[dict] = []

    def add(area: str, key: str, status: str, msg: str) -> None:
        items.append({"area": area, "item": key, "status": status, "detail": msg})

    # ---------------- owner inputs
    cfg = Path(settings_path) if settings_path else Path("config/user.yaml")
    add("owner", "settings file", OK if cfg.exists() else MISSING,
        f"{cfg} found" if cfg.exists() else f"{cfg} not found (copy config/user.example.yaml); defaults in use")
    ua = s.sec_user_agent or ""
    add("owner", "sec_user_agent", OK if ("@" in ua and " " in ua.strip()) else MISSING,
        "set (name + contact email)" if "@" in ua else "required by SEC fair access and FRED: 'Your Name you@example.com'")
    pf = None
    if s.active_portfolio:
        pf = one(app.conn, "SELECT * FROM portfolio WHERE name=? OR id=?", (s.active_portfolio, s.active_portfolio))
    add("owner", "active_portfolio", OK if pf else MISSING,
        f"{pf['name']} ({pf['kind']})" if pf else "set active_portfolio to the side-account portfolio")
    if pf:
        accts = all_rows(app.conn, "SELECT name, tax_status FROM account WHERE portfolio_id=?", (pf["id"],))
        excluded = [a["name"] for a in accts if a["tax_status"] in s.no_company_research_tax_statuses]
        unknown = [a["name"] for a in accts if a["tax_status"] == "UNKNOWN"]
        add("owner", "side-account scope", MISSING if not accts else WARN if (excluded or unknown) else OK,
            "no account" if not accts else
            (f"accounts {excluded} are retirement (no company research) — the active portfolio should be the side account"
             if excluded else f"tax status UNKNOWN for {unknown}" if unknown else
             ", ".join(f"{a['name']} {a['tax_status']}" for a in accts)))
        n_ev = one(app.conn, "SELECT COUNT(*) AS n FROM ledger_event e JOIN account a ON a.id=e.account_id WHERE a.portfolio_id=?",
                   (pf["id"],))["n"]
        add("owner", "transactions / opening positions", OK if n_ev else MISSING, f"{n_ev} ledger events" if n_ev else
            "import broker history or opening positions (`eqm import transactions`)")
        snap = one(app.conn, "SELECT MAX(s.as_of_date) AS t FROM brokerage_snapshot s JOIN account a ON a.id=s.account_id "
                             "WHERE a.portfolio_id=?", (pf["id"],))["t"]
        add("owner", "broker reconciliation snapshot", OK if snap else MISSING,
            f"latest {snap}" if snap else "import a brokerage snapshot (`eqm import snapshot`) to reconcile")
    c = s.contribution
    add("owner", "monthly contribution", OK if c.monthly_amount_usd else MISSING,
        f"{c.monthly_amount_usd} USD/month (not cash until recorded as a DEPOSIT)" if c.monthly_amount_usd
        else "set contribution.monthly_amount_usd (e.g. 1000)")
    add("owner", "SCHG relationship", OK if c.schg_relationship != "undecided" else MISSING,
        c.schg_relationship if c.schg_relationship != "undecided" else "decide: supplement or replace SCHG purchases")
    add("owner", "portfolio limits reviewed", OK if s.risk.confirmed else MISSING,
        "risk.confirmed: true" if s.risk.confirmed else "review config/policy.yaml limits, then set risk.confirmed: true")
    pol = app.policy.status
    add("owner", "policy status", OK if pol in ("APPROVED", "FROZEN") else MISSING,
        f"{pol}" + ("" if pol != "PREVIEW" else ": approve (`eqm policy approve`); FREEZE before paper tracking"))
    counts = {k: one(app.conn, q)["n"] for k, q in {
        "approved theses": "SELECT COUNT(DISTINCT thesis_version_id) AS n FROM thesis_approval",
        "approved valuations": "SELECT COUNT(DISTINCT valuation_version_id) AS n FROM valuation_approval",
        "approved exposure profiles": "SELECT COUNT(DISTINCT exposure_version_id) AS n FROM exposure_approval"}.items()}
    for k, n in counts.items():
        add("owner", k, OK if n else MISSING, f"{n}" if n else "none yet: purchases stay unavailable until approved")
    bm = s.benchmarks
    add("owner", "primary benchmark", OK if bm and bm[0] == "SPY" else WARN,
        f"{bm[0]} (S&P 500) primary; also {', '.join(bm[1:]) or 'none'}" if bm else "no benchmark configured")
    preview_reasons = [r for r, cond in (("policy PREVIEW", pol == "PREVIEW"), ("risk not confirmed", not s.risk.confirmed),
                                         ("portfolio not ACTUAL", bool(pf) and pf["kind"] != "ACTUAL"),
                                         ("no active portfolio", not pf)) if cond]
    add("owner", "output label", INFO if preview_reasons else OK,
        "PREVIEW (" + "; ".join(preview_reasons) + ")" if preview_reasons else "personalized output (not PREVIEW)")

    # ---------------- integrations (presence only; values never shown)
    llm = s.llm
    if llm.provider == "none":
        add("integration", "LLM", OK, "provider none (deterministic only; recommended for the first pilot)")
    elif llm.provider == "anthropic":
        sdk = importlib.util.find_spec("anthropic") is not None
        add("integration", "LLM SDK", OK if sdk else MISSING, "installed" if sdk else "`uv sync --extra llm`")
        add("integration", "ANTHROPIC_API_KEY", OK if _present("ANTHROPIC_API_KEY") else WARN,
            "present in environment" if _present("ANTHROPIC_API_KEY") else
            "not in environment (an `ant auth login` profile may also work; verify with one authorized call)")
        add("integration", "LLM budget", OK if llm.monthly_budget_usd else MISSING,
            f"{llm.monthly_budget_usd} USD/month (worst-case reservation per call)" if llm.monthly_budget_usd
            else "llm.monthly_budget_usd is required for paid calls")
    else:
        add("integration", "LLM", INFO, f"provider {llm.provider} (no real inference)")
    n = s.notifications
    url = _present(n.webhook_url_env)
    st = OK if (n.webhook_enabled and n.webhook_authorized and url) else INFO if not n.webhook_enabled else WARN
    add("integration", "notifications", st,
        f"webhook enabled={n.webhook_enabled} authorized={n.webhook_authorized} {n.webhook_url_env}="
        + ("present" if url else "absent") + ("; local inbox only" if st == INFO else "")
        + ("; send one test with `eqm alerts test` and confirm receipt" if st == OK else ""))
    last = one(app.conn, "SELECT MAX(started_at) AS t FROM job_run")["t"]
    add("integration", "scheduler", OK if last else WARN,
        f"last job run {last}" if last else "never ran: start `eqm serve` (or cron/systemd) on an always-on machine")
    bdir = app.home / "backups"
    backups = sorted(bdir.glob("eqm_backup_*.tar.gz")) if bdir.exists() else []
    add("integration", "backups", OK if backups else WARN,
        f"{len(backups)} in {bdir}, latest {backups[-1].name}" if backups else f"none in {bdir} (`eqm backup create --dest {bdir}`)")
    missing = [i for i in items if i["status"] == MISSING]
    return {"home": str(app.home), "items": items, "missing": len(missing),
            "ready_for_live_pilot": not [i for i in missing if i["item"] in ("sec_user_agent",)],
            "note": "Secrets are reported as present/absent only. Live integrations are not exercised by this check."}
