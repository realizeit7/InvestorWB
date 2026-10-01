# Operational runbook

## Runtime requirements

Monitoring only happens while something runs the scheduler. A laptop that sleeps or a stopped process cannot monitor.
Choose one:

**A. Long-running process** (simplest; e.g. a small always-on machine):
```bash
uv run eqm serve --poll 60          # runs due jobs, then delivers pending notifications, every 60 s
```
systemd unit (`/etc/systemd/system/eqm.service`):
```ini
[Unit]
Description=equity-monitor scheduler
After=network-online.target
[Service]
WorkingDirectory=/path/to/InvestorWB
Environment=EQM_HOME=/path/to/InvestorWB/var
EnvironmentFile=-/path/to/InvestorWB/.env
ExecStart=/usr/bin/env uv run eqm serve --poll 60
Restart=always
[Install]
WantedBy=multi-user.target
```

**B. cron** (every 15 minutes; each run is idempotent). cron starts with an almost empty environment, so load the
env file explicitly — otherwise `EQM_WEBHOOK_URL` is missing and nothing is delivered:
```cron
*/15 * * * * cd /path/to/InvestorWB && uv run eqm --env-file /path/to/InvestorWB/.env jobs run-due >> var/cron.log 2>&1
*/15 * * * * cd /path/to/InvestorWB && uv run eqm --env-file /path/to/InvestorWB/.env alerts deliver >> var/cron.log 2>&1
```

### Environment variables per launch method

The application **never reads `.env` by itself**. Variables used: `EQM_HOME` (data directory), `EQM_WEBHOOK_URL`
(or the name set in `notifications.webhook_url_env`), `ANTHROPIC_API_KEY` (only if `llm.provider: anthropic`).
Keep the file outside git (`.env` is git-ignored), owner-readable only: `chmod 600 .env`. Format: `KEY=value` lines.

| launch method | how variables reach the process |
|---|---|
| interactive shell | `set -a; source .env; set +a` before `uv run eqm ...`, or pass `--env-file .env` to each command |
| any `eqm` command | `uv run eqm --env-file /abs/path/.env <command>` — loaded before argument defaults; variables already set in the environment win; values are never printed |
| `eqm serve` under systemd | `EnvironmentFile=/abs/path/.env` in the unit (systemd syntax: `KEY=value`, no `export`); or `ExecStart=... eqm --env-file /abs/path/.env serve` |
| cron | `eqm --env-file /abs/path/.env ...` in each line (cron does not source shell profiles) |
| macOS launchd | `EnvironmentVariables` dict in the plist, or `ProgramArguments` including `--env-file /abs/path/.env` |
| Docker / hosted runner | the platform's secret/env mechanism (`docker run --env-file`, provider secrets); never bake secrets into images |

Verify with `uv run eqm --env-file /abs/path/.env setup check`: it lists every required owner input and reports secrets
only as present/absent.

Check: `eqm jobs status` (last/next run; a due-but-not-run instance is shown as MISSED) and `eqm health`
(warns "scheduler has never run" or "was due ... but has not run").

## Job semantics

- Each job instance has key `<job>@<scheduled time UTC>`; re-running a finished instance is a no-op; a crashed/failed
  instance is retried up to 3 attempts; a RUNNING instance older than 2 h is treated as interrupted.
- Only the latest missed instance of each job runs after downtime (no replay storm).
- `daily_refresh`: prices → SEC filing index (+ facts when a 10-K/10-Q appears) → market context (reference ETFs/indices/futures,
  FRED series, FINRA short interest/short-sale volume, shared snapshot) → recommendations with purchase eligibility →
  events/alerts → delivery. Failures end the job PARTIAL with a HEALTH alert; missing market inputs appear as MISSING/UNKNOWN.
- `eqm jobs run daily_refresh --force` re-runs the latest instance manually.

## Notifications

Local inbox (`eqm alerts list|show|ack|snooze`, dashboard `/inbox`) is always on. External delivery (one webhook adapter)
stays **off** until all three are true: `notifications.webhook_enabled: true`, `notifications.webhook_authorized: true`
in `config/user.yaml`, and `EQM_WEBHOOK_URL` set in the environment. The JSON body carries an `idempotency_key` and the
same value in the `Idempotency-Key` header. Timeouts after sending are AMBIGUOUS: by default the row is HELD; inspect the
receiver, then `eqm alerts requeue <outbox_id>` if it did not arrive. DEAD rows (permanent 4xx or max attempts) need a
configuration fix. Notifications never execute trades.

## Backup and restore

```bash
uv run eqm backup create --dest backups        # consistent SQLite copy + raw store + SHA-256 manifest (.tar.gz)
uv run eqm --home var_restored backup restore --archive backups/eqm_backup_<stamp>.tar.gz
uv run eqm --home var backup restore --archive <file> --force   # existing DB is renamed *.pre-restore-*, never deleted
```
Restore verifies every checksum and runs `PRAGMA integrity_check`. Keep backups off the machine (the database holds your
holdings). Suggested: nightly cron `eqm backup create` + weekly copy elsewhere; test a restore monthly.

## Credentials and secrets

- SEC: `sec_user_agent: "Your Name you@example.com"` in `config/user.yaml` (not a secret, but personal).
- LLM: `llm.provider: anthropic` + `uv sync --extra llm` + credentials the Anthropic SDK resolves (`ANTHROPIC_API_KEY`
  or an `ant auth login` profile). `llm.monthly_budget_usd` is **required** for paid calls: each request reserves a
  conservative worst-case cost before it is sent and is refused if that would exceed the budget (POLICY §12). It is
  a pre-authorization limit, not a provider-enforced cap — also set a spending limit in the Anthropic console.
- Webhook URL: environment variable only (`EQM_WEBHOOK_URL`), e.g. from a git-ignored `.env` loaded as described in
  *Environment variables per launch method* (it is not read implicitly).
- `config/user.yaml`, `var/` and `.env` are git-ignored.

## Troubleshooting

| symptom | action |
|---|---|
| REVIEW with `FILINGS_NOT_CHECKED` | the daily job has not run in 36 h or SEC failed: `eqm jobs status`, `eqm health` |
| REVIEW with `STALE_PRICE` / `DATA_REFRESH_FAILED` | provider down or symbol changed: `eqm prices refresh --symbols X`; consider the CSV provider |
| HTTP 403 from SEC | set a User-Agent with an email address |
| FRED series MISSING / "FRED needs a User-Agent" | set `sec_user_agent` (FRED stalls requests without a contact email) |
| Purchases PAUSED `NO_APPROVED_EXPOSURE_PROFILE` | `eqm exposure draft/create/approve SYMBOL` |
| Purchases PAUSED `UNREVIEWED_MATERIAL_EVENT` | read the filing; record a decision (`eqm decide`) or re-approve thesis/valuation |
| HTTP 429 from Yahoo | wait; the adapter retries with backoff; failures stay visible |
| `UNSUPPORTED_CORPORATE_ACTION` | record the actual outcome (reverse/replace or new OPENING_POSITION with basis), then `eqm reconcile --resolve <id> --note ...` |
| `NEGATIVE_CASH` | add the missing deposit or opening cash balance |
| `NEW_FINANCIALS_SINCE_VALUATION` | `eqm valuation build X`, review, `eqm valuation approve X --downside-reviewed` |
