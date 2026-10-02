# Supervised first-use (pilot) checklist

Scope: InvestorWB researches the **$1,000/month side account** only. It never trades, never gives individual-company
recommendations for the 401(k) (accounts marked `TAX_DEFERRED` are refused), and never sells or liquidates retirement
money. The primary comparison is a contribution-matched **S&P 500 (SPY)** benchmark; SCHG and VTI are shown as well.
Outputs stay **PREVIEW** until the owner items below are supplied. The software has **no demonstrated stock-selection
advantage**; that can only be measured prospectively against the passive benchmark.

Run `uv run eqm --env-file /abs/path/.env setup check` at any time: it lists what is still missing and reports secrets
only as present/absent.

## A. Owner inputs (only you can supply these)

| # | item | where | status in repo |
|---|---|---|---|
| A1 | Your name + contact email for SEC/FRED (`sec_user_agent`) | `config/user.yaml` | not supplied |
| A2 | Side-account portfolio + account with tax status `TAXABLE`; set `active_portfolio` | `eqm portfolio create/add-account` | not supplied |
| A3 | Broker transactions or opening positions (CSV) | `eqm import transactions` | not supplied |
| A4 | A broker snapshot for reconciliation | `eqm import snapshot` | not supplied |
| A5 | Monthly contribution `1000` | `contribution.monthly_amount_usd` | not supplied |
| A6 | Supplement or replace SCHG purchases | `contribution.schg_relationship` | undecided |
| A7 | Review portfolio limits (10% issuer, 30% sector, 8% target, 10–15 holdings); then `risk.confirmed: true` | `config/policy.yaml`, `config/user.yaml` | not confirmed |
| A8 | Approve the policy; FREEZE it before paper tracking | `eqm policy approve` | PREVIEW |
| A9 | Theses, valuation assumptions and exposure profiles per company (with evidence review where flagged) | `eqm thesis/valuation/exposure approve` | none |
| A10 | (optional) LLM without an API key. Default: keep `llm.provider: none` and use `eqm finder pack` + a Claude Code session. CLI judging: install Claude Code on the always-on machine, log in once (`claude`), verify your plan / auth route / billing settings, run `eqm llm claude-check` until PASS, then set `llm.provider: claude_code` (and `finder_auto_judge: true` for the weekly job). (API alternative: `anthropic` + `monthly_budget_usd` + `ANTHROPIC_API_KEY`) | runtime machine | not configured |
| A10b | Company finder: try `eqm finder run` once; if useful, set `finder_enabled: true` for the weekly scan | `config/user.yaml` | off by default |
| A11 | Notification destination compatible with a generic JSON webhook (or ask for an adapter), `EQM_WEBHOOK_URL`, then `webhook_enabled` + `webhook_authorized` | `.env` + `config/user.yaml` | not configured |
| A12 | An always-on machine and how it is launched (systemd / cron / launchd / hosted) | docs/RUNBOOK.md | not chosen |
| A13 | Backup destination off the machine | your storage | not chosen |

Never paste keys into chat or commit them. The app does not read `.env` implicitly — see RUNBOOK *Environment variables
per launch method*.

## B. Implemented and tested (offline suite, `uv run pytest`)

Ledger/import/reconciliation, PIT fundamentals, debt aggregation, screening, DCF, evidence verification (citation
integrity vs support; roles, directions, periods; legacy downgrades; evidence reviews), recommendation engine,
allocation (re-validation at cutoff, issuer aggregation, fees), paper execution (cash, limits, eligibility, frozen
policy, per-book identity), benchmarks (inception replay, SPY primary), alerts (eligibility transitions), exposure
evidence re-checks, conservative LLM spend reservations, side-account scope guard, setup check, explicit env file,
company finder (universe, SIC peers, average-rank ties, deep score, conservative gap + sensitivity, arms A–D frozen as
cohorts, LLM judgment via interactive pack or checked Claude Code, executable-timing evaluation vs SPY with coverage and
the evidence gate), Claude Code isolation check (offline, real CLI),
scheduler idempotency and interrupted-run handling, backup/restore. See VALIDATION.md.

## C. Live checks actually run (2026-10-01, separate data home, placeholder SEC contact, no LLM)

`scripts/live_pilot.sh` + `scripts/pilot_ops_check.py` — results in VALIDATION.md §8:

| check | result |
|---|---|
| SEC EDGAR filings + XBRL facts (MSFT, KO) | ran |
| Yahoo chart prices (holdings + SPY/SCHG/VTI) | ran |
| FRED, FINRA, 19 reference instruments → snapshot | ran |
| review / allocation / performance (SPY primary) / scheduler pass / health | ran; nothing purchasable without approvals (expected) |
| restart/recovery (kill -9 during a job, restart, retry after stale window; `serve` stop/start) | ran |
| delivery mechanics to a LOCAL receiver on 127.0.0.1 | ran (not the owner's destination) |
| backup → restore into a new home → integrity + row counts | ran |

## D. Not yet run (must not be reported as done)

| item | why | how the owner runs it |
|---|---|---|
| Owner-authorized notification to your real destination, received on your phone | needs your URL and authorization | set A11, then `uv run eqm --env-file .env alerts test`; confirm receipt; `eqm alerts ack <id> --note received` |
| Claude Code check + live call under your login | must run on your machine with your account; needs your authorization | `uv run eqm llm claude-check` (offline), then `--live --i-authorize-one-live-call`; then `eqm finder judge` on one shortlist |
| Prospective finder evaluation | needs ≥ 24 months and ≥ 52 matured 126-session cohorts before any performance claim | keep the weekly finder running under a frozen `finder.protocol_version`; `eqm finder evaluate` |
| Always-on scheduling on your machine for days | needs your host | install per RUNBOOK; check `eqm health` daily for a week |
| Real holdings reconciliation | needs A3/A4 | `eqm reconcile` until no open issues |
| Prospective paper tracking (augmented vs baseline vs SPY) | starts after A8 FROZEN | one PAPER book per variant, fund identically, `eqm paper` after each monthly proposal |

## E. After the pilot

Begin prospective paper tracking under a FROZEN policy and compare with the contribution-matched SPY benchmark over
time. Treat results as descriptive until a separately specified research-validation milestone defines the test.
Passing software tests establish behaviour, not profitability.
