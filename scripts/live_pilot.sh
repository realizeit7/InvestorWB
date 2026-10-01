#!/usr/bin/env bash
# Supervised live-data pilot in a SEPARATE data home (never your real var/), no LLM, nothing approved, PREVIEW output.
# Uses the example CSVs as an ILLUSTRATIVE HYPOTHETICAL side account (SCHG/MSFT/KO) — not your holdings.
#   EQM_SEC_USER_AGENT="Your Name you@example.com" scripts/live_pilot.sh /path/to/pilot_home
set -uo pipefail
HOME_DIR="${1:?usage: live_pilot.sh <separate pilot data home>}"
: "${EQM_SEC_USER_AGENT:?set EQM_SEC_USER_AGENT to 'Your Name you@example.com' (SEC/FRED contact string)}"
mkdir -p "$HOME_DIR"
SETTINGS="$HOME_DIR/pilot_user.yaml"
cat > "$SETTINGS" <<YAML
sec_user_agent: "$EQM_SEC_USER_AGENT"
active_portfolio: pilot-side
llm:
  provider: none
notifications:
  webhook_enabled: false
  webhook_authorized: false
YAML
EQM=(uv run eqm --home "$HOME_DIR" --settings "$SETTINGS" --policy config/policy.example.yaml)
step() { echo; echo "=== $*"; }
run() { echo "+ eqm ${*}"; "${EQM[@]}" "$@"; echo "  (exit $?)"; }

step "1. setup check before any data"; run setup check
step "2. illustrative side account (HYPOTHETICAL, TAXABLE)"
run portfolio create pilot-side --kind HYPOTHETICAL
run portfolio add-account brokerage --portfolio pilot-side --tax-status TAXABLE
run import transactions brokerage examples/transactions.example.csv
run import snapshot brokerage examples/snapshot.example.csv --as-of 2026-02-28
step "3. live SEC filings + XBRL facts (no key needed)"; run sec sync MSFT; run sec sync KO
step "4. live prices (Yahoo chart) incl. SPY/SCHG/VTI benchmarks"; run prices refresh --portfolio pilot-side
step "5. live market context (FRED, FINRA, reference ETFs) -> shared snapshot"; run market refresh --portfolio pilot-side
step "6. valuation draft (NOT approved) and review"; run valuation build MSFT --reason "pilot draft"
run review --portfolio pilot-side
step "7. allocation (expected: nothing purchasable without owner approvals)"; run allocate --portfolio pilot-side
step "8. performance vs contribution-matched benchmarks (SPY primary)"; run performance --portfolio pilot-side --start 2026-01-02
step "9. scheduler pass, health, setup check"; run jobs run-due; run health; run setup check
