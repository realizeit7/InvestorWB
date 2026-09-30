# VALIDATION — what actually ran (2026-09-30)

Environment: Linux cloud container, Python 3.11.15, uv 0.8.17, branch `claude/equity-monitor-foundation`.
This report covers **software acceptance only**. Nothing here is evidence of investment performance.

## 1. Automated tests

`uv run pytest` → **80 passed, 0 failed** (≈3 s). The tests use synthetic fixtures and hand-computed expectations and
need no network, API key or paid inference.

| spec §18 item | covered by (tests/…) |
|---|---|
| 1 re-import creates no duplicates | `test_ledger.py::test_reimport_same_csv_creates_no_duplicates`, `test_identical_rows_within_one_file_are_distinct_events` |
| 2 deposits, sales, fees, dividends, DRIP, splits reconcile | `test_ledger.py::test_full_cycle_reconciles` (hand-computed cash 8,543, basis 1,275.25, realized 748.25, NAV 10,124; T+1 unsettled cash) |
| 3 unknown basis ≠ zero | `test_ledger.py::test_unknown_cost_basis_is_not_zero` |
| 4 unsupported corporate actions → issues | `test_unsupported_corporate_action_creates_issue`, `test_provider_split_without_ledger_event_is_flagged` |
| 5 restatements cannot leak backwards | `test_valuation_fundamentals.py::test_restatement_does_not_leak_into_earlier_as_of` |
| 6 after-close filing cannot affect earlier recommendation | `test_decisions.py::test_after_close_filing_cannot_affect_earlier_recommendation` |
| 7 valuation = hand-worked example | `test_valuation_fundamentals.py::test_dcf_matches_hand_worked_example` (EV 1,930.9091; 17.009091/share) |
| 8 invalid terminal growth fails | `test_invalid_terminal_growth_fails` |
| 9 debt, cash, dilution effects | `test_debt_cash_dilution_effects` (incl. SBC double-count guard) |
| 10 quarterly/YTD + units | `test_quarterly_from_ytd_and_units`, `test_weighted_shares_are_never_derived_by_subtraction` |
| 11 invalid denominators not attractive | `test_decisions.py::test_invalid_denominators_do_not_look_attractive` |
| 12 weaker fundamentals reduce value at the same price | `test_weaker_fundamentals_reduce_value_with_price_unchanged` |
| 13 price rise without value change stops additions | `test_price_rise_without_value_change_stops_additions`, `test_rerun_without_changes_is_idempotent_and_changes_are_explained` |
| 14 price fall + broken thesis ≠ ADD | `test_price_fall_with_broken_thesis_never_adds` |
| 15 missing critical evidence → REVIEW | `test_missing_critical_evidence_gives_review` (12 parametrized cases) |
| 16 unsupported/forged citations cannot drive decisions | `test_citation_verification`, `test_llm_assessed_trigger_is_ambiguous_not_exit`, `test_llm_event_assessment_leads_to_review` |
| 17 allocation respects budget/issuer/sector/rounding | `test_allocation_respects_constraints_and_keeps_cash` |
| 18 unused cash stays unallocated | same test + `test_hypothetical_contribution_is_labelled` |
| 19 recommendations don't modify holdings | `test_all_five_states_end_to_end_and_holdings_untouched` |
| 20 actual/paper/fixture cannot mix | `test_ledger.py::test_actual_paper_fixture_cannot_mix`, paper test in `test_evaluation.py` |
| 21 scheduler restart no duplicate events | `test_monitoring.py::test_restart_does_not_duplicate_events`, `test_interrupted_run_is_retried_not_duplicated` |
| 22 failed refresh → visible warnings | `test_failed_refresh_is_visible`, `test_scheduler_not_running_is_reported` |
| 23 delivery retries obey dedup | `test_delivery_retries_and_dedup`, `test_ambiguous_timeout_is_held`, `test_permanent_error_and_max_attempts`, `test_delivery_disabled_without_authorization` |
| 24 calendar & DST boundaries | `test_calendar_ops.py` (2026 holidays, observed rules, early closes, DST closes, latest session) + `test_schedule_dst_boundaries` |
| 25 benchmarks with flows & corporate actions | `test_evaluation.py::test_contribution_matched_benchmark_hand_computed`, `test_total_return_index_no_double_counting` |
| 26 injected instructions cannot change policy or act | `test_prompt_injection_cannot_change_policy_or_trigger_actions` |
| 27 all five states on labelled fixtures | `test_all_actions_from_engine`, `test_all_five_states_end_to_end_and_holdings_untouched` |
| 28 thesis history & overrides auditable | `test_original_and_current_thesis_are_auditable`, `test_override_is_recorded_separately`, `test_correction_is_append_only` |

Other coverage: backup/restore round-trip with checksum verification, hysteresis bands, limit-breach trims, cooldown never
suppressing CRITICAL alerts, acknowledge/snooze audit log, IRR no-solution/multiple-root cases, TWR flow neutrality, paper
fills at the next open (never earlier), example CSV files import and reconcile cleanly, SIC→sector mapping, dashboard
pages render with labels, CSRF rejection, and CLI smoke paths.

## 2. Live checks against real services (network available in this session)

| check | result |
|---|---|
| SEC EDGAR submissions + companyfacts for AAPL, MSFT, KO, PEP, CAT, JPM | OK: 29–203 filings indexed per issuer, 2.8k–4.4k facts normalized each |
| `acceptanceDateTime` time zone | verified UTC (Apple's 2026-07-30 earnings 8-K = 16:30 ET); an initial ET assumption was **wrong** and was corrected |
| 10-K/10-Q text extraction | Apple 10-K 165 passages; MSFT 10-K 261 + 10-Q 162 |
| Citation verification on real text | two real quotes and one XBRL fact VERIFIED; a fabricated passage FAILED |
| Yahoo chart prices | ~276 daily bars per symbol; raw prices recovered around NVDA's 2024-06-10 10:1 split (1208.88 → 121.79) |
| Screening | 5 ranked; JPM excluded as BANK_OR_CREDIT |
| DCF + reverse DCF (AAPL, MSFT) | ran; e.g. AAPL at $329.40 implies ~27% constant 5-yr revenue growth at a 9% WACC. ILLUSTRATIVE only, not approved |
| Full CLI workflow in a scratch home (example CSV → snapshot → SEC link/sync → prices → valuation → review → allocation → jobs → health → backup) | all commands succeeded; re-import added 0 rows; jobs second run `ALREADY_DONE`; health OK |
| Local dashboard in headless Chromium (desktop, dark mode, 390 px phone) | rendered; no horizontal page overflow |

Bugs found by the live runs and fixed (each with a regression test): YTD subtraction applied to weighted-average share
counts; missing D&A tags; "10-K" misread as the number 10 in claims; first filings sync would flood the inbox with
historical filings; CSV-created securities could not be linked to SEC issuers; SIC 35xx machinery mapped to Technology; a valuation approval made after an as-of time was visible to that earlier decision.

The SEC checks used a placeholder development User-Agent (`InvestorWB-dev devtest@example.com`). Your own contact
string belongs in `config/user.yaml`.

## 3. Not run / not verified

- **LLM calls were not executed**: no API key in this environment. The Anthropic adapter is written against the SDK
  documentation but is untested live; the LLM flows were tested with a deterministic fixture provider.
- **External notification delivery was not sent anywhere**: tested with fake transports only; disabled by default.
- **Scheduling is not running**: no persistent process was left running; `eqm serve` / cron must be set up by you.
- Historical point-in-time universe and delisted securities: not available (no paid provider), so no historical claims.
- Your real holdings, tax status, limits and contribution plan: not supplied; all personalized output is PREVIEW.
- Performance evaluation was exercised on fixtures only; there is no live record.
