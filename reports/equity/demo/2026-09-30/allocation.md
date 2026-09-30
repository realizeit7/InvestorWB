# Monthly allocation proposal — 2026-09-30

> **FIXTURE** — synthetic demonstration data. Not market evidence; companies are fictional.
> **PREVIEW** — policy thresholds are provisional and unapproved, risk settings not confirmed, fixture portfolio. Not personalized advice.
> Decision support only: nothing here places orders. Proposed trades are proposals, not executions.

- Contribution basis: **CONFIRMED**
- Budget: $51,760.00 (deployable settled cash $51,760.00)
- NAV before/after contribution: $103,003.56 / $103,003.56 (after fees $103,003.56)
- Proposed weight = aggregate issuer weight (all share classes, current + proposed) after rounding and fees.

| Rank | Symbol | MoS | Current weight | Proposed $ | Est. shares | Fee | Proposed weight | Binding constraint |
|---|---|---|---|---|---|---|---|---|
| 1 | ZZADD | 40.0% | 3.1% | $5,040.84 | 222.06 | $0.00 | 8.0% | TARGET_WEIGHT  |

**Remaining unallocated cash: $46,719.16**

Excluded candidates:

- ZZEXT: action at cutoff is EXIT (VERIFIED_THESIS_INVALIDATION)
- ZZHLD: action at cutoff is HOLD (NO_ADD)
- ZZTRM: action at cutoff is TRIM (VALUATION_ABOVE_TRIM_BAND)
- ZZREV: action at cutoff is REVIEW (VALUATION_NOT_APPROVED): valuation assumptions not approved
- ZZNEW: ADD but purchases PAUSED: ADVERSE_REFINANCING_HIGH_EXPOSURE (reassess 2026-10-30)

Notes:

- Household-level concentration is UNKNOWN: retirement/outside holdings are not recorded.
- Whether this contribution replaces or supplements SCHG purchases is undecided (setting).
- 46719.16 left unallocated because constraints bound (TARGET_WEIGHT).

Decisions re-validated at the cutoff (current policy and evidence):

- ZZEXT: EXIT / purchases BLOCKED — rec_b62dd2d702214d3a9e68 (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZHLD: HOLD / purchases ELIGIBLE — rec_6339d8d91c744c0597ad (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZTRM: TRIM / purchases BLOCKED — rec_9748e8d7144e4c40a76b (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZADD: ADD / purchases ELIGIBLE — rec_8bbd6dd6a1ea4017a76c (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZREV: REVIEW / purchases BLOCKED — rec_38c5c343e1b74cddb12f (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZNEW: ADD / purchases PAUSED — rec_105a8e1f08b8471c99b8 (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)

Record your decision with `eqm decide --allocation <id> ACCEPT|REJECT|OVERRIDE`. After trading, record the actual fills with `eqm ledger add` or a CSV import.
