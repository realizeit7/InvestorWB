# Post-Selloff Recovery Research — protocol `sr-0.1` (PROVISIONAL)

Research question: *can information available after a negative event distinguish excessive price declines from
justified reductions in business value?* First event category: **clinical-trial failure** — evaluating the remaining
business, financing and valuation **after** a failure becomes public, never predicting trial outcomes.

This file and `config/selloff_protocol.yaml` (the machine-readable rules; its hash is stored with every research run)
were committed **before any event's prices or outcomes were fetched**. All values are **provisional research
choices** for a data-feasibility study of about 20–30 events; none is validated, and the sample is for testing
document and outcome reconstruction, not profitability. Scope: the owner's $1,000/month side-account research; the
401(k) stays outside company analysis. Nothing here trades, notifies or writes to the portfolio database.

## 1. Observation period — 2019-01-01 … 2023-12-31 (announcement date, US Eastern)
- From 2019 the 8-K cover page states each class's **trading symbol and exchange**, so the ticker valid at the time
  can be read from the filing itself (EDGAR search results show *today's* names and tickers, e.g. Aerpio → AADI).
- All filers report XBRL financial statements in this period, so cash, debt and cash use are available point in time.
- Ending in 2023 leaves ≥ 252 later sessions for every event, so outcome coverage can be audited.
- EDGAR full-text search covers 2001 onward, so an extension to 2017–2018 is possible if too few events qualify.

## 2. Eligible issuers and securities
Domestic SEC registrants (10-K/10-Q filers) with SIC 2833–2836 or 8731; other SIC codes stay visible and are included
only if the filing shows a drug developer. Common stock listed on Nasdaq, NYSE or NYSE American **as stated on the
8-K cover page at the time**; OTC listings excluded. Foreign private issuers (6-K/20-F) are out of scope because their
announcements are not 8-K filings (a known coverage gap).

## 3. Phases and categories
Cohort: **PRIMARY_ENDPOINT_FAILURE** — the company announces that a Phase 2, 2b, 2/3 or 3 trial of its own program
(including a partner-run trial of its own asset, flagged) **did not meet its prespecified primary endpoint**. Missing
the primary endpoint while meeting secondary endpoints is still this category (flagged).
Recorded and visible but **not** in the sr-0.1 cohort, never merged with it: FUTILITY_STOP, SAFETY_STOP,
COMMERCIAL_DISCONTINUATION, REGULATORY_REJECTION, MIXED_OR_UNCLEAR, NOT_AN_EVENT (a mention of an older or another
company's failure). Phase 1 and 1/2 trials are excluded (different information content and investor base).

## 4. Discovery (historical filings, not today's survivors)
SEC EDGAR full-text search over **8-K filings and their exhibits**, one query per main phrase and calendar year
(eight phrases, listed in the YAML), all result pages retrieved, every query and its reported total stored. The pool
is every distinct accession returned. Starting from historical filings means companies that were later acquired,
delisted or bankrupt are in the pool.

**Known incompleteness (not exhaustive):** announcements made only by press release (no 8-K), different wording
("did not reach statistical significance", "not statistically significant"), images or PDFs without searchable text,
foreign private issuers, and EDGAR search errors. Three *sensitivity phrases* (YAML) are run but never sampled: the
number of additional issuers they find is reported as a lower bound on what the main phrases miss.

## 5. Sampling and stopping (decided before screening)
- Order: ascending `sha256("sr-0.1:" + accession)` — reproducible and unrelated to outcomes.
- Screen accessions in that order; each screening decision is recorded with the category, a verbatim quote and the
  reason (included or not).
- Stop after **30** events pass the document-based screen or after **200** accessions. If fewer than **20** pass,
  document an extension (2017–2018) before any outcome is inspected.
- Price-based criteria (decline, market cap, price) are applied only **after** the stop, so they never influence
  which filings are screened. Recovery is never a selection criterion.

## 6. Deduplication
Event key = CIK + trial identifier (NCT number if stated, else drug + indication + phase). Filings about the same
readout within 30 days are one event, timed at the earliest public time. Later full-data or conference presentations of
the same readout are not new events. The same drug in a different trial is a separate event, flagged as a repeat.

## 7. Price decline (eligibility, not a return study)
Decline = close of the first session that **closes after the latest possible public time** ÷ close of the last
session that **closes before the earliest possible public time** − 1. Eligible if ≤ **−30%**; the SPY- and
XBI-relative moves over the same window are recorded too. Also required: pre-event market cap ≥ $50M (point-in-time
shares outstanding × pre-event close) and pre-event price ≥ $1. If prices are unavailable the event is
**ELIGIBILITY_UNDETERMINED**: visible, counted, never excluded silently or assumed.
Rationale: about 30% is far outside a small-cap biotech's usual daily range and is a common threshold for
"significant repricing"; $50M and $1 avoid untradeable micro-caps. All three are provisional.

## 8. Timing convention (never invent intraday precision)
Four times are kept apart: when the event occurred (if stated), when a document was submitted (EDGAR acceptance,
exact), when information became public (earliest verified source; a release dateline is **date-only** → earliest
possible = 00:00 ET that date), and when this system retrieved it.
- **Decision cutoff** = close of the decline-measurement session + 120 minutes (the signal needs the completed day).
- **Entry** = opening price of the first session that opens after the cutoff.
- A session without a bar (halt) is replaced by the first session with a bar, and the event is flagged.

## 9. Point-in-time evidence
Only document versions public at the cutoff: the event 8-K and exhibits, the latest 10-Q/10-K, and earlier filings
describing the pipeline and financing. XBRL facts are used as originally filed up to the cutoff — later restatements
are recorded as "later revised" but never used. Never substituted: today's web pages, later restatements, current
trial status, later articles. ClinicalTrials.gov **historical record versions are not retrievable** here (history
endpoint 403, page rendered client-side), so only the current record may be used, and only for stable identifiers,
labelled CURRENT_VERSION_NOT_POINT_IN_TIME. A known earlier announcement that cannot be recovered is recorded as a gap.

## 10. Missing data, records and LLM use
Missing values are `None` with a reason, never zero. A missing outcome is neither a zero return nor a total loss.
Structured records separate FACT (cited, verified), ASSUMPTION and OPINION. Cash-runway figures state formula,
period and assumptions; historical cash use is not a forecast. No numerical recovery probabilities. The operating-
company DCF is **not** applied to pre-revenue companies (marked UNSUPPORTED_VALUATION); previous highs or averages are
never fair value. LLM assistance runs through interactive evidence packs only (no API or unattended CLI calls). Any
present-day LLM judgment of a historical event is labelled **RETROSPECTIVE_CONTAMINATED** — the model may know what
happened later, and hiding names does not fix this. Screening decisions in this milestone were made with LLM
assistance under the same caveat; they are constrained to verbatim-quoted document facts, and every decision
(including exclusions) is recorded.

## 11. Outcome coverage audit only
For each event: whether data exist for the entry price, the 21/63/126/252-session horizons, drawdowns, corporate
actions, SPY and XBI over matching dates, and evidence of acquisition (8-K 2.01, DEFM14A, SC TO-T), bankruptcy (8-K
1.03) or delisting (Form 25, Form 15). **No forward returns are computed in sr-0.1.**

## 12. Later evaluation paths (not run in this milestone)
1. *Historical quantitative evaluation*: explicit rules and point-in-time inputs, executable timing, costs and
   unavailable outcomes; filtered candidates vs **all eligible selloff events**, SPY and XBI; chronological development
   with an untouched evaluation period.
2. *Prospective judgment evaluation*: LLM and owner judgments saved before outcomes occur, with the evidence pack,
   model/prompt version and timestamp frozen; later edits create new cohorts; judgment must add value beyond the
   quantitative baseline.
