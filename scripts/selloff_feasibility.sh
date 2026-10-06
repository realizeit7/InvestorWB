#!/usr/bin/env bash
# Post-Selloff Recovery Research — data-feasibility run (protocol sr-0.1), from an EMPTY research home.
# Research-only: never opens the portfolio database. Public sources only (SEC EDGAR, Yahoo chart); no paid data, no LLM.
#   EQM_SEC_USER_AGENT="Your Name you@example.com" scripts/selloff_feasibility.sh /path/to/research_home [settings.yaml]
set -uo pipefail
RH="${1:?usage: selloff_feasibility.sh <research home> [settings.yaml]}"
SETTINGS="${2:-}"
if [ -z "$SETTINGS" ]; then
  : "${EQM_SEC_USER_AGENT:?set EQM_SEC_USER_AGENT to 'Your Name you@example.com'}"
  mkdir -p "$RH"; SETTINGS="$RH/research_user.yaml"
  printf 'sec_user_agent: "%s"\nllm:\n  provider: none\n' "$EQM_SEC_USER_AGENT" > "$SETTINGS"
fi
EQM=(uv run eqm --settings "$SETTINGS" study selloff)
FAILED=()
run() { echo "+ eqm study selloff $*"; "${EQM[@]}" "$@" --research-home "$RH"; local rc=$?; echo "  (exit $rc)";
        [ "$rc" -ne 0 ] && FAILED+=("$* (exit $rc)"); return 0; }
run init                                                    # isolated home + protocol hash
run discover                                                # every protocol query, all pages, logged
run record --file docs/selloff/screening_decisions.json     # committed screening decisions (quotes re-verified)
run run                                                     # events, PIT sources, XBRL financing, prices, coverage
run pack --out "$RH/packs"                                  # point-in-time evidence packs (interactive workflow)
run import-judgments --file docs/selloff/retrospective_judgments.json   # 3 RETROSPECTIVE_CONTAMINATED test judgments
run coverage --out "$RH/coverage_sr-0.1.csv"
if [ "${#FAILED[@]}" -gt 0 ]; then printf 'FAILED: %s\n' "${FAILED[@]}"; exit 1; fi
echo "OK: research home $RH (no portfolio data touched)"
