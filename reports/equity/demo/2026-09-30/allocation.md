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

- ZZTRM: action at cutoff is TRIM (VALUATION_ABOVE_TRIM_BAND)
- ZZEXT: action at cutoff is EXIT (VERIFIED_THESIS_INVALIDATION)
- ZZREV: action at cutoff is REVIEW (VALUATION_NOT_APPROVED): valuation assumptions not approved
- ZZHLD: action at cutoff is HOLD (NO_ADD)
- ZZNEW: ADD but purchases PAUSED: ADVERSE_REFINANCING_HIGH_EXPOSURE (reassess 2026-10-30)

Notes:

- Household-level concentration is UNKNOWN: retirement/outside holdings are not recorded.
- Whether this contribution replaces or supplements SCHG purchases is undecided (setting).
- 46719.16 left unallocated because constraints bound (TARGET_WEIGHT).

Decisions re-validated at the cutoff (current policy and evidence):

- ZZTRM: TRIM / purchases BLOCKED — rec_6aa868eae24746b6a551 (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZNEW: ADD / purchases PAUSED — rec_80fb95a718d24e3d9a62 (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZEXT: EXIT / purchases BLOCKED — rec_1419fa250e1242ff9989 (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZADD: ADD / purchases ELIGIBLE — rec_0b08c315197542adb2de (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZREV: REVIEW / purchases BLOCKED — rec_f15d23fc72594a1fa528 (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)
- ZZHLD: HOLD / purchases ELIGIBLE — rec_bf164652ac83467d97d5 (equivalent earlier review reused as of 2026-09-30T22:00:00.000000Z)

Record your decision with `eqm decide --allocation <id> ACCEPT|REJECT|OVERRIDE`. After trading, record the actual fills with `eqm ledger add` or a CSV import.
