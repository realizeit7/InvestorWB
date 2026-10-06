# Post-Selloff Recovery Research — data-feasibility report (protocol `sr-0.1`)

Research question: *can information available after a negative event distinguish excessive price declines from
justified reductions in business value?* Event category in this milestone: clinical-trial **primary-endpoint
failure** (Phase 2–3). This milestone tested whether events, documents, timing, financing and outcome **data** can be
reconstructed point in time. It did **not** compute forward returns, build a model, rank stocks or test a strategy.
**No profitability or investment-advantage claim is made or implied.** Nothing here touches the portfolio database,
recommendations, allocations or approvals.

Reproduce (public sources, ~3 minutes, separate research home):
`EQM_SEC_USER_AGENT="Name email" scripts/selloff_feasibility.sh /path/to/empty/research_home`.
Protocol committed before any event price was fetched: `d53d4c5` (`config/selloff_protocol.yaml`,
`docs/selloff/PROTOCOL.md`). Screening decisions: `docs/selloff/screening_decisions.json`. Coverage table (one row per
event): `docs/selloff/coverage_sr-0.1.csv`. Implementation notes and shared-component findings:
`docs/selloff/IMPLEMENTATION_NOTES.md`.

## 1. Funnel (actual results of the final run)

| step | result |
|---|---|
| discovery | 55 EDGAR full-text queries (8 phrases × 5 years + 3 sensitivity phrases × 5 years), all pages, 0 errors after retries (the service returned intermittent HTTP 500s that retries absorbed) |
| pool | 286 distinct 8-K accessions from 137 issuers (2019–2023) |
| screened | 70 accessions in the protocol's fixed hash order; stop rule met at rank 70 (30th event) |
| included | **30 events** (PRIMARY_ENDPOINT_FAILURE, Phase 2/2b/2/3/3) |
| excluded, visible | 33 NOT_AN_EVENT (29 are results releases or presentations recalling an earlier announcement), 4 primary-endpoint failures whose filing states no phase, 1 Phase 1/2b, 1 dose-specific mixed result, 1 device trial |
| categories never seen | FUTILITY_STOP, SAFETY_STOP, COMMERCIAL_DISCONTINUATION, REGULATORY_REJECTION — the discovery phrases target primary endpoints, so these categories are **not covered** by this sample |
| quote verification | 70/70 screening quotes found verbatim in the filings (re-verified in the reproducible run) |

## 2. What could be reconstructed reliably

| item | coverage (30 events) |
|---|---|
| stable issuer identity (SEC CIK) | 30/30 |
| ticker valid at the time | 29/30 — 27 from the 8-K cover page, 1 from a 10-Q cover filed before the cutoff, 1 from the press release text; **1 unknown** (ImmunoGen, March 2019, before cover pages listed symbols). EDGAR search shows *today's* names and tickers (e.g. Aerpio → AADI) and is never used for identity |
| document submitted (EDGAR acceptance, exact, UTC — checked against SEC's own index page) | 30/30 |
| earliest possible public time | 30/30 **date-only**: every event has a release dateline or 8-K "date of earliest event reported", so the window starts at 00:00 New York of that date; 12 events have a decline window longer than one session because the date-only bound precedes the filing. No intraday precision was invented |
| decision cutoff and first executable session | 30/30 (cutoff = close of the first session closing after the filing + 120 min; entry = next open) |
| event documents (8-K + every EX-99 exhibit) with URL, accession, acceptance, retrieval time, raw hash, parser version, passages | 30/30 |
| latest 10-Q and 10-K accepted by the cutoff | 29/30 (one company had not yet filed a 10-K) |
| next periodic report after the cutoff recorded as **excluded** | 30/30 |
| cash, shares outstanding (XBRL as filed by the cutoff) | 30/30 |
| historical cash-use runway with stated formula, period and assumptions | 30/30 (2 "not burning cash on this basis") |
| price history, entry open and 21/63/126/252-session availability | **19/30** (15 by the ticker at the time, 4 by the same CIK's current ticker after a rename) |
| SPY and XBI over matching dates | 30/30 |
| outcome status evidence from later filings | 30/30 classified: 9 subject-side acquisition filings, 2 acquisition-related filings of unknown role, 3 delisting/deregistration, 16 still filing |

## 3. What could not be reconstructed (or only partly)

- **Prices of acquired and delisted companies.** All 11 unpriced events are companies with acquisition or delisting
  evidence or a reused ticker: 8 return no data from the free source and 3 were caught by the identity guard because
  the ticker now belongs to a different listing (GNMX and ONTX → ETFs, RAIN → another company). The free source drops
  delisted symbols entirely; Stooq and Nasdaq were checked and do not fill the gap. **The missing outcomes are
  systematically the acquired/delisted companies** — exactly the outcomes a recovery study most needs.
- **Price eligibility.** 10 eligible (declines −34% to −83%), 9 ineligible (8 with declines smaller than 30%, two of
  them also below a price or market-cap floor; 1 below the $1 price floor despite a −69% decline), 11 undetermined
  (no prices). Every eligible event is therefore a company whose price history
  survives in the free source.
- **Intraday timing.** No source gives the press-release time; all 30 events are date-only at their earliest bound.
- **ClinicalTrials.gov history.** Historical record versions are not retrievable here (history endpoint 403, page
  rendered client-side). Only 2 filings stated an NCT number; trial identity otherwise rests on drug + indication + phase.
- **Debt and investments.** Long-term debt usable for 3/30 (21 not found under the shared concept map, 6 stale — a tag
  last reported years earlier); long-term investments 0/30 (not in the shared concept map); revenue 19/30 (9 not found,
  2 stale). Values are recorded as unknown with the reason, never zero.
- **Later restatements.** The mechanism works (tested), but no restated value was detected for these 30 events, so the
  restatement path is untested on live data.
- **Remaining business, pipeline and catalysts.** Keyword extraction proposed 169 sentences (all quote-verified), but
  with low precision — mostly the failed trial itself. A usable record needs human or LLM review per event.
- **Judgments.** Only 3 workflow-test judgments were written (by an LLM, from the packs), all labelled
  **RETROSPECTIVE_CONTAMINATED**. They test the workflow and verification; they are not evidence of anything.

## 4. Sampling and source biases

1. **Wording bias.** The 8 main phrases found 137 issuers; the 3 sensitivity phrases found 99, of which **63 were not
   in the main pool**. How many of those had a qualifying event is unknown (not screened), but the main phrases clearly
   miss a large share of announcements.
2. **Filing-channel bias.** Announcements made only by press release, or by foreign private issuers (6-K), are absent.
   Of 29 recall releases, 22 had an earlier filing of the same issuer in the pool; 7 did not — consistent with originals
   using other wording or no 8-K.
3. **Survivorship in outcomes.** Outcome data exist only for survivors and renamed survivors (section 3).
4. **Category bias.** Only primary-endpoint failures were found; safety, futility and regulatory events need their own
   discovery phrases.
5. **Screener contamination.** Screening was done by an LLM-assisted session that may know later outcomes. Mitigations:
   fixed hash order, decisions limited to verbatim document facts, every decision (incl. 40 exclusions) recorded,
   price criteria applied only after the stop rule. The risk is reduced, not eliminated.
6. **Size.** 30 events, 10 price-eligible: a feasibility sample only. No statistic from it would be meaningful.

## 5. Effort per event

| step | automated (measured, wall-clock incl. rate-limited network) | manual |
|---|---|---|
| discovery (all 30) | ~40 s total | none |
| screening | filing retrieval ~1 s per filing | reading and deciding with a verbatim quote: an LLM-assisted session handled 70 filings; a human would need an **estimated** 3–6 min per filing (≈ 4–7 h for 70), more for ambiguous recaps |
| events, sources, XBRL financing, statements, prices, coverage | ~3 s per event (≈ 1.5 min for 30) | none |
| evidence pack review and structured judgment (sections A–D) | pack export < 1 s | **estimated** 30–60 min per event for a human; the 3 LLM-written test judgments each needed the full pack (40–70 KB) and still left debt and revenue gaps open |

Manual estimates are estimates, not measurements.

## 6. Additional data needed for a larger study (none purchased)

1. **Survivorship-free daily prices with delisting returns and corporate actions** for US equities 2018–present (e.g. a
   CRSP-type dataset or a commercial EOD vendor with delisted coverage). Required — without it, outcomes are missing
   exactly for acquired and delisted companies.
2. **Press-release timestamps** (newswire archives) to replace date-only bounds and shorten decline windows.
3. **Broader discovery:** the sensitivity phrases, 6-K filings, and newswire search for releases without an 8-K; plus
   separate phrases for safety, futility and regulatory events.
4. **Historical trial registry versions** (ClinicalTrials.gov history through an accessible channel).
5. **A wider XBRL concept map** for convertible notes, long-term investments and collaboration revenue (shared
   component; see implementation notes).

## 7. Is a larger historical study justified?

**Not with free data alone.** Discovery, screening with verified quotes, point-in-time documents, exact filing times
and financing records reconstruct reliably and cheaply. But outcomes are unavailable for 11 of 30 events, and the
missing ones are systematically the acquired and delisted companies. Any return study on the remaining survivors would
be biased in a direction that cannot be bounded from these data.

A larger study is justified **only if** the owner decides to license survivorship-free price data with delisting
returns (item 6.1); broadened discovery (6.3) is strongly recommended in the same step. That is an owner decision about
paid access, so this milestone stops here. If it is approved, the next step is the quantitative path in PROTOCOL.md §12:
a new protocol version fixed before any returns are examined, comparison against **all** eligible selloff events, SPY
and XBI, and an untouched evaluation period. Prospective judgment collection (frozen packs, model/prompt versions and
timestamps) can start independently, because it does not depend on historical prices.

## 8. Status of this milestone

| item | status |
|---|---|
| protocol committed before prices/outcomes | done (`d53d4c5`) |
| isolated research home and research-only commands (`eqm study selloff …`) | done; portfolio database byte-identical in tests |
| event registry (30 events) with traceable evidence and timing confidence | done |
| source manifest and point-in-time evidence pack per event | done (30 packs) |
| structured financial and event records | done for financing (XBRL) and stated failure; remaining business and recovery thesis only as candidates or 3 contaminated test judgments |
| coverage table | done (`coverage_sr-0.1.csv`) |
| feasibility report | this document |
| offline tests | `tests/test_selloff.py` (19 tests), full suite green |
| **blocked** | survivorship-free prices (paid), ClinicalTrials.gov history (403), press-release times |
| **not run (by design)** | forward returns, drawdowns, strategy tests, prospective judgments |
