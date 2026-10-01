# VALIDATION — what actually ran (2026-09-30)

Environment: Linux cloud container, Python 3.11.15, uv 0.8.17, branch `claude/equity-monitor-foundation`.
This report covers **software acceptance only**. Nothing here is evidence of investment performance.

## 1. Automated tests

`uv run pytest` → **99 passed, 0 failed** (≈11 s) at v1.0/v1.1; **137 passed, 0 failed** (≈13 s) after the §5 correctness repair; **150 passed, 0 failed** (≈23 s) after the §6 follow-up repair; **159 passed, 0 failed** (≈23 s) after the §7 repair; **175 passed, 0 failed** (≈17 s) after §8. The tests use synthetic fixtures and hand-computed expectations and
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
| 16 unsupported/forged citations cannot drive decisions | `test_citation_verification`, `test_repairs.py` §1 tests, `test_llm_assessed_trigger_is_ambiguous_not_exit`, `test_llm_event_assessment_leads_to_review` |
| 17 allocation respects budget/issuer/sector/rounding | `test_allocation_respects_constraints_and_keeps_cash`, `test_share_classes_share_one_issuer_limit_in_both_variants`, `test_proposal_is_revalidated_after_fees_and_rounding` |
| 18 unused cash stays unallocated | same test + `test_hypothetical_contribution_is_labelled` |
| 19 recommendations don't modify holdings | `test_all_five_states_end_to_end_and_holdings_untouched` |
| 20 actual/paper/fixture cannot mix | `test_ledger.py::test_actual_paper_fixture_cannot_mix`, paper test in `test_evaluation.py` |
| 21 scheduler restart no duplicate events | `test_monitoring.py::test_restart_does_not_duplicate_events`, `test_interrupted_run_is_retried_not_duplicated` |
| 22 failed refresh → visible warnings | `test_failed_refresh_is_visible`, `test_scheduler_not_running_is_reported` |
| 23 delivery retries obey dedup | `test_delivery_retries_and_dedup`, `test_ambiguous_timeout_is_held`, `test_permanent_error_and_max_attempts`, `test_delivery_disabled_without_authorization` |
| 24 calendar & DST boundaries | `test_calendar_ops.py` (2026 holidays, observed rules, early closes, DST closes, latest session) + `test_schedule_dst_boundaries` |
| 25 benchmarks with flows & corporate actions | `test_evaluation.py::test_contribution_matched_benchmark_hand_computed`, `test_total_return_index_no_double_counting`, `test_subperiod_benchmark_keeps_earlier_funding` |
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
| Citation verification on real text | two real quotes and one XBRL fact VERIFIED; a fabricated passage FAILED (run with the pre-repair matcher; under §5 rules the quotes are SOURCE_MATCHED unless every statement is confirmed field by field — not re-run live) |
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

## 4. v1.1 market-context amendment (2026-09-30)

`tests/test_market.py` (19 tests) plus updated monitoring/dashboard/fixture tests. Requirement → test:

| requirement | test |
|---|---|
| irrelevant events do not alter recommendations (oil shock, energy selloff, broad selloff for a software company: same recommendation id, action and eligibility) | `test_irrelevant_events_do_not_alter_recommendations` |
| overlapping evidence is not counted independently (8-K + short interest + short volume + headline + price move = one cluster, one pause) | `test_overlapping_company_evidence_is_one_development` |
| same market risk not counted twice (RATES+REFINANCING from one flag = one chain, one pause, one WACC proposal; market beta once at portfolio level) | `test_same_market_risk_is_not_counted_twice` |
| missing data stays explicit (missing 10y/2y with HIGH rate exposure → UNKNOWN → PAUSED; no profile → PAUSED) | `test_missing_data_is_unknown_not_safe`, `test_no_approved_exposure_profile_pauses` |
| changed decisions traceable to sources and policy versions | `test_changed_decisions_are_traceable`, `test_reviews_share_one_snapshot` |
| HOLD + PAUSED coexist; pause has reason, reassess condition/date; allocation skips paused names; baseline comparison recorded | `test_adverse_development_on_high_exposure_pauses_but_does_not_sell` |
| strength is not a buy signal; weakness never liquidates; market-stress pause is opt-in | `test_favorable_development_is_not_a_buy_signal`, `test_market_weakness_never_liquidates_and_stress_pause_is_opt_in` |
| high short interest → research only, never sell | `test_high_short_interest_is_research_not_sell` |
| interpretation guards (short-sale volume ≠ short interest, volume ≠ flows, IV ≠ probability, futures OI/prices ≠ forecasts, options activity ≠ intent) | `test_interpretation_guards` |
| market context never bypasses limits | `test_market_context_never_bypasses_limits` |
| exposure evidence or assumption label; unique factors; verified fact citations | `test_exposure_needs_evidence_or_assumption_label` |
| publication times and revisions point-in-time | `test_series_point_in_time_and_revisions` |
| provider parsers & publication-lag rules offline | `test_provider_parsers_offline` |
| LLM explanations are context only; unknown observation references rejected | `test_llm_explanations_are_context_only` |
| baseline vs augmented paper variants (FROZEN policy, next-open fills) | `test_paper_variants_for_prospective_comparison` |

Live checks (2026-09-30, `scripts/e2e_market_context.py AAPL`): 19 reference instruments (SPY, QQQ, 11 sector ETFs, VIX, VIX3M,
TLT, HYG, CL=F, HG=F) and 13 FRED series loaded; FINRA short interest (AAPL, settlement 2026-09-15) and Reg SHO short-sale volume
loaded; real snapshot flags RATES_UP (10y +0.51pp/1m) and OIL_UP (WTI +29%/3m), WTI marked STALE (last obs 8 days old); the rate
flag linked to Apple's LOW refinancing exposure → NO_CHANGE, oil → context (no exposure path). Found and fixed live: FRED silently
stalls requests whose User-Agent lacks a contact email (the job hung for minutes) → FRED now uses the configured contact string,
fails fast without one, and has a 15 s timeout; approving an exposure profile no longer counts as reviewing a company filing; the drafted refinancing claim now cites each filed debt figure (a computed total could not verify).

Not run: single-stock options, ETF flows/holdings (no reliable free source), CFTC positioning (deferred), ALFRED vintages (needs a
FRED key), LLM cluster explanations against a real model (no key; tested with a fixture model), prospective baseline-vs-augmented
outcomes (no live record yet).

## 5. Correctness repair of a2d0ef8 (2026-09-30)

Scope: the seven review findings plus the evaluation corrections. No ML, feeds, deployment, purchases, notifications or
trades were added. Every finding was first reproduced on a2d0ef8 with a scratch script (outputs below), then covered by a
regression test in `tests/test_repairs.py` (38 tests). **All 38 fail when run against a2d0ef8** (with imports of new
APIs stubbed so each test is collected) and pass after the repair. Full suite: **137 passed, 0 failed**, offline, no
credentials.

| # | finding | before (a2d0ef8, reproduced) | after | tests |
|---|---|---|---|---|
| 1 | citation match labelled as verification | "Revenue **decreased** 12% to $4.2 billion", "…$4.2 **trillion**" and "The company is insolvent" all VERIFIED against "Revenue increased 12% to $4.2 billion…" | FAILED (direction), FAILED (scale), SOURCE_MATCHED (free text); thesis/exposure approval and engine gates respect it | `test_confirmed_numeric_claim_is_verified_on_every_field`, `test_contradicted_claims_fail` ×5 (direction, scale ×2, period, value), `test_sign_change_fails`, `test_unrelated_and_free_text_claims_are_never_verified`, `test_fact_citations_check_metric_and_period`, `test_llm_support_opinion_never_upgrades`, `test_thesis_approval_respects_verification`, `test_approved_thesis_with_failed_claim_is_review`, `test_legacy_verified_claims_are_downgraded_to_source_matched` |
| 2 | issuer limit exceeded across share classes | ZZADD $5,040.84 + ZZADD.B $5,040.84 → issuer weight **12.89%** (limit 10%, target 8%), same in baseline | ZZADD $5,040.84 only → **8.00%**; both variants; revalidated after fees/rounding; displayed weight is aggregate | `test_share_classes_share_one_issuer_limit_in_both_variants[True/False]`, `test_proposal_is_revalidated_after_fees_and_rounding` |
| 3 | stale recommendations reused (≤ 7 days) | two days after the review, allocation proposed ZZADD $5,040.84 and ZZNEW $8,240.28 although a fresh review is REVIEW (STALE_PRICE, FILINGS_NOT_CHECKED) | nothing proposed; each exclusion names the cutoff review's codes; future cutoffs refused | `test_allocation_revalidates_stale_recommendations`, `test_allocation_sees_changed_eligibility_and_new_evidence`, `test_allocation_uses_current_policy_and_rejects_future_cutoff` |
| 4 | paper execution | unfunded paper book bought ZZADD+ZZNEW, cash **−13,281.12**; a PAUSED ADD filled directly (cash −999.99); TRIM always sold half | unfunded book buys nothing, cash 0; direct ADD refused; per-variant eligibility, cash, fees, rounding, paper-book limits; TRIM to documented target weight; bound to the originating FROZEN policy; atomic and idempotent | `test_paper_allocation_never_borrows`, `test_paper_allocation_charges_fees_and_respects_share_rounding`, `test_paper_allocation_is_idempotent_and_atomic`, `test_paper_respects_variant_eligibility`, `test_record_events_can_refuse_negative_cash`, `test_paper_trim_sells_to_documented_target_weight` |
| 5 | debt normalization | LongTermDebtCurrent 100 and DebtCurrent 300 folded into one concept; only the first tag's value (100) used; missing components silently 0 and labelled FACT | separate concepts; total 1,100 (noncurrent 800 + DebtCurrent 300, current maturities not double counted); missing parts unknown and flagged; approval needs `--accept-assumptions` | `test_total_and_component_are_not_double_counted`, `test_separate_short_term_borrowings_and_current_maturities_are_both_counted`, `test_missing_debt_components_stay_unknown`, `test_debt_components_from_different_dates_are_not_combined`, `test_screening_treats_incomplete_debt_as_unknown`, `test_valuation_with_assumptions_needs_explicit_acceptance`, `test_legacy_debt_concepts_are_remapped_by_source_tag` |
| 6 | subperiod benchmark drops earlier funding | September benchmark ended at **0.00** against a NAV of 103,003.56 | 105,760.78 (replayed from inception, then sliced); `rebased` mode separately labelled | `test_subperiod_benchmark_keeps_earlier_funding`, `test_withdrawal_larger_than_benchmark_empties_it`, `test_missing_benchmark_bars_execute_late_or_stay_unapplied` |
| 7 | alerts ignore eligibility transitions | ADD/ELIGIBLE → ADD/PAUSED produced **no alert** | "ZZADD: purchases ELIGIBLE → PAUSED" with pause code, evidence and reassess condition; PAUSED → ELIGIBLE alerted; unchanged reruns silent | `test_eligibility_transitions_raise_explained_alerts_once`, `test_monthly_allocation_revalidation_alerts_changed_eligibility` |
| — | augmented evaluation | repeated rows counted as separate pauses; withheld cash summed across proposals; returns "until today"; ≥ 20 rows → "readout allowed" | episodes; per-proposal cash (never summed); fixed 63-session horizon; descriptive verdict only | `test_augmented_evaluation_uses_episodes_fixed_horizon_and_no_cumulative_cash` |

Existing tests changed because the behaviour they encoded was the defect: `test_paper_fill_uses_next_open_never_earlier_price`
(ADD now goes through an allocation; the policy must be FROZEN before the decision) and
`test_paper_variants_for_prospective_comparison` (proposal re-made under the FROZEN policy); `test_market.py` evaluation key
renamed; fixtures gained explicit zero `debt_current` / `minority_interest` / `short_term_investments` facts so the fictional
balance sheets are complete rather than assumed.

Not re-run in this milestone: live SEC/Yahoo/FRED checks (no provider code changed except the debt concept split; a live
re-sync is needed to populate the new debt concepts for real issuers), LLM calls (no key), external notification delivery
(disabled). **Passing tests establish software behaviour, not profitability.**

### Research status (clarification)

The system does rules-based screening, DCF valuation and market-context conditions. Its regression price attribution is
descriptive (association), not a predictor. There is **no predictive-model training and no walk-forward backtest
pipeline** in this repository. The paper-execution and performance tools measure what a frozen policy would have done
prospectively; they establish no edge, and no fixture output is performance evidence.

## 6. Follow-up repair of af00fc1 (2026-10-01)

The three remaining findings were reproduced on af00fc1 with `scripts/repro_review_af00fc1.py`, then covered by 13 new
tests in `tests/test_repairs.py` (50 in that file; full suite **150 passed, 0 failed**, offline, no credentials). The 12
tests for R1–R3 were run against af00fc1: **all 12 fail there** (11 on behaviour; one imports the new `_book_state`
helper) and pass now; the 38 tests from §5 still pass on both. The 13th test covers the ev-2 claim downgrade migration.

| # | finding | before (af00fc1) | after | tests |
|---|---|---|---|---|
| R1 | claim verifier accepts reversed relationships | source "Revenue increased from $3 billion to $4 billion in 2025": "…from **$4 billion to $3 billion**…" VERIFIED; "Revenue was **$3 billion** in 2025" VERIFIED | FAILED (internally inconsistent / value contradicted) and FAILED (comparison value is not the 2025 level); the correct claims stay VERIFIED; unreadable roles and unstated prior periods are SOURCE_MATCHED; thesis approval blocks or requires acknowledgement accordingly | `test_swapped_from_to_values_fail`, `test_comparison_value_presented_as_current_result_fails`, `test_prior_and_current_periods`, `test_levels_versus_changes`, `test_relationship_failures_reach_the_approval_gates`, `test_claims_verified_by_previous_verifier_are_downgraded` |
| R2 | two proposals in one session breach paper limits | two proposals into one $3,000 book: both bought ZZADD and ZZNEW, each issuer **19.994%** (limit 10%) | the second proposal (or the other variant) is refused as mutually exclusive, in either order; issuers 9.996%; same-proposal reruns still idempotent; execution state includes same-session fills valued at open-time prices (a $100 close is ignored for a $10 open fill); paper limits net of fees (a same-session holding of the other share class leaves $30 of issuer room, so nothing more is bought) | `test_two_proposals_in_one_session_cannot_breach_limits[both orders]`, `test_execution_state_includes_same_session_fills_at_open_time_prices`, `test_paper_book_limits_aggregate_share_classes_with_fees` |
| R3 | sell executed in one book suppresses the other | ZZTRM TRIM: augmented sold (6.001 left), baseline **not executed** (100 left) | both books execute (6.001 left in each), neither twice; EXIT likewise; migration 0006 keeps existing rows and moves uniqueness to (recommendation, paper portfolio) | `test_sell_recommendation_executes_once_in_each_paper_book[TRIM/EXIT]`, `test_paper_execution_scope_migration_preserves_rows` |

Found while testing R2 and fixed here: paper fees could push a paper-book weight slightly over its limit (10.02%), because
limits were sized before fees reduced NAV. They are now sized against NAV net of the maximum fees.

Not re-run: live data feeds, LLM calls, notification delivery, persistent scheduling. Passing tests establish software
behaviour, not profitability. There is still no predictive-model training/validation pipeline or walk-forward backtest;
that is a separate research-validation milestone.

## 7. Repair after the review of be46212 (2026-10-01)

Both findings were reproduced on be46212 with `scripts/repro_review_be46212.py`, then covered by 9 new tests in
`tests/test_repairs.py` (60 in that file). Full offline suite: **159 passed, 0 failed** (`uv run pytest`, no network or
credentials). The 8 tests for P1a/P1b were run against be46212: **7 fail there** — 5 on behaviour (`VERIFIED` instead of
FAILED/SOURCE_MATCHED, `ADD` instead of HOLD) and 2 because they import the new review functions; the 8th
(`test_correct_and_direction_neutral_levels_still_verify`) passes on both by design, guarding against over-correction.
The 9th covers re-verification of ev-3 claims. All 51 earlier repair tests still pass.

| # | finding | before (be46212) | after | tests |
|---|---|---|---|---|
| P1a | contradictory direction on a level verified | source "Revenue increased from $3 billion to $4 billion in 2025": "Revenue **decreased** to $4 billion in 2025" VERIFIED; source "…decreased from $5 billion to $4 billion…": "Revenue **increased** to $4 billion" VERIFIED | both FAILED (direction contradicted by the stated comparison); "increased to $4 billion" and "was $4 billion" still VERIFIED; a direction with no comparison in the evidence is SOURCE_MATCHED; with XBRL facts, citing only the 2025 fact leaves "increased to" SOURCE_MATCHED, citing 2024 and 2025 verifies it, "decreased to" FAILS | `test_level_with_contradictory_direction_fails`, `test_correct_and_direction_neutral_levels_still_verify`, `test_direction_without_comparison_evidence_is_not_verified`, `test_direction_from_cited_prior_period_fact` |
| P1b | downgraded evidence keeps its approval effective | legacy thesis with a swapped claim stored as ev-2 VERIFIED, approved; after migration 0007 the claim is SOURCE_MATCHED/LEGACY but the new recommendation is **ADD/ELIGIBLE** and `approve_version()` returns silently | new recommendation HOLD with `EVIDENCE_REVIEW_REQUIRED` (held position not sold, no trade proposed); allocation excludes it in both variants; re-approval re-verifies the claim under ev-4 → FAILED → `ThesisEvidenceError`, engine REVIEW (`THESIS_EVIDENCE_FAILED`), no review can be recorded; a legacy claim that is only SOURCE_MATCHED under ev-4 needs `--acknowledge-unverified`, which records a review against its current status (point in time; a later status change re-opens it) and restores ADD; a corrected, VERIFIED, newly approved version restores ADD and allocation; the old approval (empty note) and the old ADD recommendation remain unchanged | `test_downgraded_evidence_blocks_new_adds_without_selling`, `test_existing_approval_cannot_silently_satisfy_the_review`, `test_acknowledged_review_is_recorded_against_current_status`, `test_corrected_verified_thesis_restores_eligibility`, `test_legacy_ev3_claim_reverifies_on_reapproval`, `test_claims_verified_by_previous_verifier_are_downgraded` (updated for 0008) |

Reproduction output after the repair:
`[P1a] decreased-to vs increased source: FAILED; increased-to vs decreased source: FAILED; increased-to: VERIFIED; was: VERIFIED`
`[P1b] claim SOURCE_MATCHED/LEGACY; approval still on record; new recommendation HOLD; approving again: ThesisEvidenceError`.

Limits: the evidence-review gate covers thesis claims; exposure-profile versions keep the verification snapshot recorded
at their creation (exposure evidence affects purchase eligibility, not the ADD action, and approval still refuses FAILED
evidence). Claims are re-verified only when a version is re-approved or reviewed, not automatically. Not re-run: live
feeds, LLM calls, notification delivery, scheduling. Passing tests establish software behaviour, not investment
performance; no predictive model or walk-forward backtest exists in this repository.

## 8. Review of a1a330d + supervised-pilot readiness (2026-10-01)

Full offline suite: **175 passed, 0 failed** (`uv run pytest`, no network or credentials). 16 new tests in
`tests/test_repairs.py` (76 in that file). Against a1a330d all 16 fail — 3 on behaviour (profile snapshots carried no verifier version; `SCHG`
was the first benchmark; the stale-run test ended at attempt 5 instead of 1), the rest because the APIs they exercise did
not exist. The two
review findings were therefore also reproduced **behaviourally** with `scripts/repro_review_a1a330d.py`, which uses only
APIs present on both commits:

| # | finding | a1a330d (actual output) | after (actual output) | tests |
|---|---|---|---|---|
| P1 | exposure approval trusts an obsolete verification snapshot | approved legacy profile whose claim ("Revenue decreased to $4 billion", source says increased) fails under ev-4: purchases **ELIGIBLE**, no pause; an unapproved legacy profile is **approved** | **PAUSED** (`EXPOSURE_EVIDENCE_FAILED`); approval **refused** ("failed verification"); legacy evidence that still verifies is re-checked automatically and needs no human; unresolved evidence pauses (`EXPOSURE_EVIDENCE_REVIEW_REQUIRED`) until an acknowledged check of the exact statuses; profile versions and approvals unchanged, re-checks are append-only `exposure_evidence_check` rows (migration 0009) | `test_obsolete_exposure_approval_cannot_support_purchases`, `test_legacy_exposure_approval_requires_correction_or_review`, `test_legacy_exposure_evidence_that_still_verifies_needs_no_human`, `test_new_profiles_record_their_verifier_version` |
| P2 | LLM budget is not a cap | unpriced model, $100 budget: **3 requests sent, $0 counted**; $0.01 budget: **1 request sent, $0.24 counted** | **0 requests sent** in both cases (refused before sending); paid calls need a budget and known pricing; worst-case reservation in an exclusive transaction (a second process cannot reuse the same remainder); settlement to actual usage or full reservation when unknown; unknown earlier spend blocks. Documented as a conservative pre-authorization limit, not a provider-enforced cap | `test_unpriced_model_is_refused_before_sending`, `test_paid_calls_need_a_budget_and_a_worst_case_reservation`, `test_settlement_reconciles_actual_usage_and_keeps_unknowns_charged`, `test_fallbacks_reserve_the_most_expensive_model_twice`, `test_concurrent_processes_cannot_spend_the_same_remaining_budget`, `test_unknown_legacy_llm_cost_blocks_paid_calls` |

Pilot-readiness features (tests): side-account scope — TAX_DEFERRED (401(k)) portfolios get no company recommendations or
allocations and jobs skip them (`test_retirement_portfolio_gets_no_company_recommendations`); SPY primary
contribution-matched benchmark (`test_sp500_is_the_primary_contribution_matched_benchmark`); `eqm setup check` never prints
secrets (`test_setup_check_reports_presence_without_printing_secrets`); explicit `--env-file`
(`test_env_file_is_explicit_and_existing_variables_win`); owner-initiated `eqm alerts test` sends nothing unless enabled +
authorized (`test_alert_test_sends_nothing_without_authorization`).

### Live pilot actually run (separate data home, placeholder SEC contact `InvestorWB-dev devtest@example.com`, no LLM)

`scripts/live_pilot.sh <home>` on a fresh home, with the example CSVs as an ILLUSTRATIVE HYPOTHETICAL side account
(SCHG/MSFT/KO, not the owner's holdings). All 16 commands exited 0:

| step | actual result |
|---|---|
| SEC EDGAR | MSFT 81 filings, 4,480 facts; KO 145 filings, 3,830 facts |
| Yahoo prices | MSFT, KO, SPY, SCHG, VTI ok, latest 2026-09-30 |
| market context | 19 reference instruments, FRED, FINRA short interest + short-sale volume; no failures; snapshot built |
| broker snapshot reconciliation | 2 discrepancies recorded from the example files (KO quantity, cash) — ledger unchanged, KO → REVIEW |
| review | MSFT and KO REVIEW (no approved thesis/valuation), PREVIEW labels |
| allocation | nothing purchasable; $3,228.50 stays cash (expected without owner approvals) |
| performance (illustrative, 2026-01-02 → 2026-09-30) | primary benchmark SPY; portfolio TWR 7.58%, SPY contribution-matched end value $9,911 vs NAV $9,516 — **illustrative data only, not evidence of anything** |
| scheduler pass + health | daily_refresh, weekly_digest, monthly_allocation SUCCESS; health OK |

`scripts/pilot_ops_check.py <home>` (nothing leaves the machine):

| check | actual result |
|---|---|
| restart/recovery | `kill -9` during `jobs run daily_refresh` left the run RUNNING; immediate restart → IN_PROGRESS (no duplicate); retry after the 2-hour stale window (simulated clock) → SUCCESS, "previous attempt interrupted"; `eqm serve` started/stopped twice → no new rows of any kind |
| delivery mechanics | `eqm alerts test` with a LOCAL receiver on 127.0.0.1 → 1 POST, `Idempotency-Key` set, title "InvestorWB test notification"; re-delivery sent nothing |
| backup → restore | archive created; restore into a new home: checksums verified, `integrity_check` ok, row counts of 10 key tables identical |

Found by the restart test and fixed: repeated forced re-runs exhausted the retry counter, so a genuinely interrupted run
then stayed RUNNING forever without any health warning. Forced re-runs of a completed instance now start at attempt 1, a
run that gives up is marked FAILED with the reason, and health warns about RUNNING rows older than the stale window
(`test_interrupted_job_is_retried_then_failed_visibly_and_forced_reruns_reset_attempts`).

**Not run (owner action required; not reported as done):** the owner-authorized notification to the owner's real
destination and receipt on the phone; LLM authentication and a live call; days of always-on scheduling on the owner's
machine; reconciliation of real holdings; prospective paper tracking. See docs/PILOT_CHECKLIST.md. Passing tests and a
successful pilot establish software behaviour, not investment performance.
