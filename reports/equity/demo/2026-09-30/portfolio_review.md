# Portfolio review — demo-fixture — 2026-09-30

> **FIXTURE** — synthetic demonstration data. Not market evidence; companies are fictional.
> **PREVIEW** — policy thresholds are provisional and unapproved, risk settings not confirmed, fixture portfolio. Not personalized advice.
> Decision support only: nothing here places orders. Proposed trades are proposals, not executions.

## Summary

- NAV: $103,003.56
- Cash: $51,760.00 (available $51,760.00, unsettled $0.00)
- Realized gain: $0.00
- Dividends: $0.00 · Fees: $0.00
- Open reconciliation issues: 0

## Holdings and current recommendation

| Symbol | Shares | Price (date) | Value | Weight | Cost basis | Unrealized | Business | Action | Purchases | MoS | Freshness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SCHG | 600.0000 | 32.08 (2026-09-30) | $19,248.00 | 18.7% | $18,240.00 | $1,008.00 | — | **ETF (tracked, no company valuation)** | — | — | price 0 session(s) old |
| ZZHLD | 396.0396 | 24.24 (2026-09-30) | $9,600.00 | 9.3% | $8,000.00 | $1,600.00 | INTACT | **HOLD** | ELIGIBLE | 10.0% | price 0 session(s) old |
| ZZTRM | 218.0550 | 39.74 (2026-09-30) | $8,665.50 | 8.4% | $5,000.00 | $3,665.50 | INTACT | **TRIM** | BLOCKED | -30.0% | price 0 session(s) old |
| ZZREV | 288.1844 | 22.67 (2026-09-30) | $6,533.14 | 6.3% | $7,000.00 | -$466.86 | INTACT | **REVIEW** | BLOCKED | 30.0% | price 0 session(s) old |
| ZZEXT | 755.6675 | 5.29 (2026-09-30) | $3,997.48 | 3.9% | $6,000.00 | -$2,002.52 | BROKEN | **EXIT** | BLOCKED | 50.0% | price 0 session(s) old |
| ZZADD | 140.9443 | 22.70 (2026-09-30) | $3,199.44 | 3.1% | $4,000.00 | -$800.56 | INTACT | **ADD** | ELIGIBLE | 40.0% | price 0 session(s) old |

## Watchlist

| Symbol | Status | Action | Purchases | MoS | Explanation |
|---|---|---|---|---|---|
| ZZNEW | APPROVED | ADD | PAUSED | 35.0% | Thesis intact; margin of safety 35.0% >= 25%; within limits. |

## Market context used by these reviews

- Shared snapshot `mks_0072e7b2e6ca4ceab314` as of 2026-09-30T22:00:00.000000Z (session 2026-09-30); full detail: `eqm market show`
- Flags (conditions, not forecasts): CREDIT_TIGHTENING
- Missing inputs (UNKNOWN, not neutral): none
- Not available: single-stock options (IV/skew/term structure/OI); ETF fund flows; ETF holdings look-through; futures positioning (CFTC COT, deferred); licensed news feed
- Portfolio market exposure counted once: beta to SPY -0.62, to QQQ -0.17 (cash weight 50.3%; beta unknown for none). Company reviews add no separate market-move penalty.

## Per-holding review

### ZZHLD: HOLD · purchases ELIGIBLE

1. **Action and purchase eligibility** — long-term action **HOLD** (business INTACT); purchases **ELIGIBLE** (fundamental-only baseline: ELIGIBLE). Ownership remains reasonable but new money is not preferred: margin of safety 10.0% < 25%; position at/above target weight 8%
2. **Thesis** — original v1 (approved 2026-09-30); current v1 (unchanged); status INTACT
3. **Broad-market developments** — CREDIT_TIGHTENING: HY OAS 6.2% (+3.20pp over 3m); SPY 1m -0.6%, 3m +0.5%, 12m +8.1%, -1.1% from 52w high; QQQ 1m +1.8%, 3m +2.8%, 12m +9.5%, -0.2% from 52w high
   - `market:CREDIT` → relevance LINKED via REFINANCING → mechanism: REFINANCING (LOW, NEGATIVE when the company's cost of refinancing debt rises): maturing debt must be refinanced at prevailing yields and spreads; net debt/EBIT 1.32 → implication: RISK → **NO_CHANGE** (LINKED_NO_CHANGE)
   - `market:EQUITY_MARKET` → relevance CONTEXT → mechanism: none applied: market strength does not justify buying and weakness does not justify selling → implication: NONE → **CONTEXT_ONLY** (BROAD_MARKET_CONTEXT)
4. **Sector / industry developments** — Consumer Staples (XLP) 3m +0.9%, vs SPY +0.4%
   - `sector:SECTOR:XLP` → relevance SECTOR_MEMBER → mechanism: none identified → implication: NONE → **CONTEXT_ONLY** (SECTOR_CONTEXT)
5. **Company-specific developments** — ZZHLD 1m +20.0%: market +0.0%, sector +0.0%, company-specific +20.0% (association, not causation)
   - `company:PRICE:2026-W40` → relevance COMPANY → mechanism: unclear: may reflect information not yet in filings, hedging, or noise → implication: RESEARCH → **RESEARCH_TASK** (LARGE_COMPANY_SPECIFIC_MOVE)
6. **Evidence** — supporting: none cited · contradicting: none cited · research tasks: LARGE_COMPANY_SPECIFIC_MOVE (company:PRICE:2026-W40)
7. **Valuation assumptions** — no changes proposed
8. **Position size and next contribution** — HOLD: keep the position; no new money preferred
9. **Missing data and freshness** — price 2026-09-30 (0 session(s) old); filings checked 0.00 h before; latest period 2026-06-30; market snapshot 2026-09-30T22:00:00.000000Z; missing: none
10. **What would change the decision** — ADD band starts at price <= 20.20 (MoS 25%) if thesis stays intact; TRIM considered at price >= 32.32 (120% of base value); EXIT considered at price >= 46.06 (bull value); EXIT if verified: Operating margin below 10% for 2 consecutive fiscal years; EXIT if verified: Loss of the largest customer contract
    - Traceability: recommendation `rec_6339d8d91c744c0597ad`, snapshot `mks_0072e7b2e6ca4ceab314`, exposure profile `exv_9d032cd2088e4facb78d`, thesis `thv_99438008607a4b4e9c8d`, valuation `val_4e73bdedd90d4f66a101`, policy `pol_90dd733085c5486e99f9`

### ZZTRM: TRIM · purchases BLOCKED

1. **Action and purchase eligibility** — long-term action **TRIM** (business INTACT); purchases **BLOCKED** (fundamental-only baseline: BLOCKED). Price is 130% of base value (> 120%).
   - blocked: long-term action is TRIM
2. **Thesis** — original v1 (approved 2026-09-30); current v1 (unchanged); status INTACT
3. **Broad-market developments** — CREDIT_TIGHTENING: HY OAS 6.2% (+3.20pp over 3m); SPY 1m -0.6%, 3m +0.5%, 12m +8.1%, -1.1% from 52w high; QQQ 1m +1.8%, 3m +2.8%, 12m +9.5%, -0.2% from 52w high
   - `market:CREDIT` → relevance LINKED via REFINANCING → mechanism: REFINANCING (LOW, NEGATIVE when the company's cost of refinancing debt rises): maturing debt must be refinanced at prevailing yields and spreads; net debt/EBIT 1.19 → implication: RISK → **NO_CHANGE** (LINKED_NO_CHANGE)
   - `market:EQUITY_MARKET` → relevance CONTEXT → mechanism: none applied: market strength does not justify buying and weakness does not justify selling → implication: NONE → **CONTEXT_ONLY** (BROAD_MARKET_CONTEXT)
4. **Sector / industry developments** — Industrials (XLI) 3m +0.9%, vs SPY +0.4%
   - `sector:SECTOR:XLI` → relevance SECTOR_MEMBER → mechanism: none identified → implication: NONE → **CONTEXT_ONLY** (SECTOR_CONTEXT)
5. **Company-specific developments** — ZZTRM 1m +73.3%: market +0.0%, sector +0.0%, company-specific +73.3% (association, not causation)
   - `company:PRICE:2026-W40` → relevance COMPANY → mechanism: unclear: may reflect information not yet in filings, hedging, or noise → implication: RESEARCH → **RESEARCH_TASK** (LARGE_COMPANY_SPECIFIC_MOVE)
6. **Evidence** — supporting: none cited · contradicting: none cited · research tasks: LARGE_COMPANY_SPECIFIC_MOVE (company:PRICE:2026-W40)
7. **Valuation assumptions** — no changes proposed
8. **Position size and next contribution** — No new purchases: long-term action is TRIM. Proposed (not executed): SELL ~4545.361222926400000000000000000000
9. **Missing data and freshness** — price 2026-09-30 (0 session(s) old); filings checked 0.00 h before; latest period 2026-06-30; market snapshot 2026-09-30T22:00:00.000000Z; missing: none
10. **What would change the decision** — ADD band starts at price <= 22.93 (MoS 25%) if thesis stays intact; TRIM considered at price >= 36.68 (120% of base value); EXIT considered at price >= 51.15 (bull value); EXIT if verified: Operating margin below 10% for 2 consecutive fiscal years; EXIT if verified: Loss of the largest customer contract
    - Traceability: recommendation `rec_9748e8d7144e4c40a76b`, snapshot `mks_0072e7b2e6ca4ceab314`, exposure profile `exv_a391ba9609754ea4a254`, thesis `thv_ee2f0ec0faca45a68666`, valuation `val_5e044fbae8c94ec8b6f1`, policy `pol_90dd733085c5486e99f9`

### ZZREV: REVIEW · purchases BLOCKED

1. **Action and purchase eligibility** — long-term action **REVIEW** (business INTACT); purchases **BLOCKED** (fundamental-only baseline: BLOCKED). Review required before any action: valuation assumptions not approved
   - blocked: long-term action is REVIEW
2. **Thesis** — original v1 (approved 2026-09-30); current v1 (unchanged); status INTACT
3. **Broad-market developments** — CREDIT_TIGHTENING: HY OAS 6.2% (+3.20pp over 3m); SPY 1m -0.6%, 3m +0.5%, 12m +8.1%, -1.1% from 52w high; QQQ 1m +1.8%, 3m +2.8%, 12m +9.5%, -0.2% from 52w high
   - `market:CREDIT` → relevance LINKED via REFINANCING → mechanism: REFINANCING (LOW, NEGATIVE when the company's cost of refinancing debt rises): maturing debt must be refinanced at prevailing yields and spreads; net debt/EBIT 1.13 → implication: RISK → **NO_CHANGE** (LINKED_NO_CHANGE)
   - `market:EQUITY_MARKET` → relevance CONTEXT → mechanism: none applied: market strength does not justify buying and weakness does not justify selling → implication: NONE → **CONTEXT_ONLY** (BROAD_MARKET_CONTEXT)
4. **Sector / industry developments** — Communication Services (XLC) 3m +0.5%, vs SPY +0.0%
   - `sector:SECTOR:XLC` → relevance SECTOR_MEMBER → mechanism: none identified → implication: NONE → **CONTEXT_ONLY** (SECTOR_CONTEXT)
5. **Company-specific developments** — ZZREV 1m -6.7%: market +0.0%, sector n/a, company-specific -6.7% (association, not causation)
   - `company:PRICE:2026-W40` → relevance COMPANY → mechanism: none identified (co-movement is not causation) → implication: NONE → **CONTEXT_ONLY** (COMPANY_CONTEXT)
6. **Evidence** — supporting: none cited · contradicting: none cited
7. **Valuation assumptions** — no changes proposed
8. **Position size and next contribution** — No new purchases: long-term action is REVIEW
9. **Missing data and freshness** — price 2026-09-30 (0 session(s) old); filings checked 0.00 h before; latest period 2026-06-30; market snapshot 2026-09-30T22:00:00.000000Z; missing: valuation assumptions not approved
10. **What would change the decision** — Resolve: valuation assumptions not approved
    - Traceability: recommendation `rec_38c5c343e1b74cddb12f`, snapshot `mks_0072e7b2e6ca4ceab314`, exposure profile `exv_dd58edc2bc114dea8cd1`, thesis `thv_b2dcda0b95494b929e29`, valuation `val_7ae323efa5ac49ca812a`, policy `pol_90dd733085c5486e99f9`

### ZZEXT: EXIT · purchases BLOCKED

1. **Action and purchase eligibility** — long-term action **EXIT** (business BROKEN); purchases **BLOCKED** (fundamental-only baseline: BLOCKED). Pre-declared invalidation condition met and verified: Operating margin below 10% for 2 consecutive fiscal years
   - blocked: long-term action is EXIT
   - blocked: thesis invalidated (BROKEN)
   - paused: ADVERSE_REFINANCING_HIGH_EXPOSURE — REFINANCING (HIGH, NEGATIVE when the company's cost of refinancing debt rises): maturing debt must be refinanced at prevailing yields and spreads; net debt/EBIT 5.95 · reassess when CREDIT_TIGHTENING clears, or owner re-approves the valuation/exposure with this condition considered (by 2026-10-30)
2. **Thesis** — original v1 (approved 2026-09-30); current v1 (unchanged); status BROKEN
3. **Broad-market developments** — CREDIT_TIGHTENING: HY OAS 6.2% (+3.20pp over 3m); SPY 1m -0.6%, 3m +0.5%, 12m +8.1%, -1.1% from 52w high; QQQ 1m +1.8%, 3m +2.8%, 12m +9.5%, -0.2% from 52w high
   - `market:CREDIT` → relevance LINKED via REFINANCING → mechanism: REFINANCING (HIGH, NEGATIVE when the company's cost of refinancing debt rises): maturing debt must be refinanced at prevailing yields and spreads; net debt/EBIT 5.95 → implication: RISK → **PAUSE_PURCHASES** (ADVERSE_REFINANCING_HIGH_EXPOSURE)
   - `market:EQUITY_MARKET` → relevance CONTEXT → mechanism: none applied: market strength does not justify buying and weakness does not justify selling → implication: NONE → **CONTEXT_ONLY** (BROAD_MARKET_CONTEXT)
4. **Sector / industry developments** — Consumer Discretionary (XLY) 3m -0.0%, vs SPY -0.5%
   - `sector:SECTOR:XLY` → relevance SECTOR_MEMBER → mechanism: none identified → implication: NONE → **CONTEXT_ONLY** (SECTOR_CONTEXT)
5. **Company-specific developments** — ZZEXT 1m -33.4%: market +0.0%, sector +0.0%, company-specific -33.4% (association, not causation)
   - `company:PRICE:2026-W40` → relevance COMPANY → mechanism: unclear: may reflect information not yet in filings, hedging, or noise → implication: RESEARCH → **RESEARCH_TASK** (LARGE_COMPANY_SPECIFIC_MOVE)
6. **Evidence** — supporting: none cited · contradicting: none cited · research tasks: LARGE_COMPANY_SPECIFIC_MOVE (company:PRICE:2026-W40)
7. **Valuation assumptions** — no changes proposed; stress: bear 3.26 → 2.53 (WACC +0.01, context only)
8. **Position size and next contribution** — No new purchases: long-term action is EXIT; thesis invalidated (BROKEN). Proposed (not executed): SELL ~3997.48110674
9. **Missing data and freshness** — price 2026-09-30 (0 session(s) old); filings checked 0.00 h before; latest period 2026-06-30; market snapshot 2026-09-30T22:00:00.000000Z; missing: none
10. **What would change the decision** — Owner may override with a documented rationale; the original thesis stays on record.; pause lifts when CREDIT_TIGHTENING clears, or owner re-approves the valuation/exposure with this condition considered
    - Traceability: recommendation `rec_b62dd2d702214d3a9e68`, snapshot `mks_0072e7b2e6ca4ceab314`, exposure profile `exv_7d392639566f49bbbf81`, thesis `thv_839fd19e3f4f45618419`, valuation `val_b9f4a3a03dca49c9b833`, policy `pol_90dd733085c5486e99f9`

### ZZADD: ADD · purchases ELIGIBLE

1. **Action and purchase eligibility** — long-term action **ADD** (business INTACT); purchases **ELIGIBLE** (fundamental-only baseline: ELIGIBLE). Thesis intact; margin of safety 40.0% >= 25%; within limits.
2. **Thesis** — original v1 (approved 2026-09-30); current v1 (unchanged); status INTACT
3. **Broad-market developments** — CREDIT_TIGHTENING: HY OAS 6.2% (+3.20pp over 3m); SPY 1m -0.6%, 3m +0.5%, 12m +8.1%, -1.1% from 52w high; QQQ 1m +1.8%, 3m +2.8%, 12m +9.5%, -0.2% from 52w high
   - `market:CREDIT` → relevance LINKED via REFINANCING → mechanism: REFINANCING (LOW, NEGATIVE when the company's cost of refinancing debt rises): maturing debt must be refinanced at prevailing yields and spreads; net debt/EBIT 0.95 → implication: RISK → **NO_CHANGE** (LINKED_NO_CHANGE)
   - `market:EQUITY_MARKET` → relevance CONTEXT → mechanism: none applied: market strength does not justify buying and weakness does not justify selling → implication: NONE → **CONTEXT_ONLY** (BROAD_MARKET_CONTEXT)
4. **Sector / industry developments** — Technology (XLK) 3m -0.0%, vs SPY -0.5%
   - `sector:SECTOR:XLK` → relevance SECTOR_MEMBER → mechanism: none identified → implication: NONE → **CONTEXT_ONLY** (SECTOR_CONTEXT)
5. **Company-specific developments** — ZZADD 1m -20.0%: market +0.0%, sector +0.0%, company-specific -20.0% (association, not causation)
   - `company:PRICE:2026-W40` → relevance COMPANY → mechanism: unclear: may reflect information not yet in filings, hedging, or noise → implication: RESEARCH → **RESEARCH_TASK** (LARGE_COMPANY_SPECIFIC_MOVE)
6. **Evidence** — supporting: none cited · contradicting: none cited · research tasks: LARGE_COMPANY_SPECIFIC_MOVE (company:PRICE:2026-W40)
7. **Valuation assumptions** — no changes proposed
8. **Position size and next contribution** — Eligible for the next monthly contribution, up to about 5,040.85 of room to the target weight
9. **Missing data and freshness** — price 2026-09-30 (0 session(s) old); filings checked 0.00 h before; latest period 2026-06-30; market snapshot 2026-09-30T22:00:00.000000Z; missing: none
10. **What would change the decision** — ADD band starts at price <= 28.38 (MoS 25%) if thesis stays intact; TRIM considered at price >= 45.40 (120% of base value); EXIT considered at price >= 61.34 (bull value); EXIT if verified: Operating margin below 10% for 2 consecutive fiscal years; EXIT if verified: Loss of the largest customer contract
    - Traceability: recommendation `rec_8bbd6dd6a1ea4017a76c`, snapshot `mks_0072e7b2e6ca4ceab314`, exposure profile `exv_f5d129a46bae41f5b43c`, thesis `thv_300ec0a783cf46f09f94`, valuation `val_38f209c0f91740a28d42`, policy `pol_90dd733085c5486e99f9`

### ZZNEW: ADD · purchases PAUSED

1. **Action and purchase eligibility** — long-term action **ADD** (business INTACT); purchases **PAUSED** (fundamental-only baseline: ELIGIBLE). Thesis intact; margin of safety 35.0% >= 25%; within limits.
   - paused: ADVERSE_REFINANCING_HIGH_EXPOSURE — REFINANCING (HIGH, NEGATIVE when the company's cost of refinancing debt rises): ILLUSTRATIVE: large bond maturity next year must be refinanced · reassess when CREDIT_TIGHTENING clears, or owner re-approves the valuation/exposure with this condition considered (by 2026-10-30)
2. **Thesis** — original v1 (approved 2026-09-30); current v1 (unchanged); status INTACT
3. **Broad-market developments** — CREDIT_TIGHTENING: HY OAS 6.2% (+3.20pp over 3m); SPY 1m -0.6%, 3m +0.5%, 12m +8.1%, -1.1% from 52w high; QQQ 1m +1.8%, 3m +2.8%, 12m +9.5%, -0.2% from 52w high
   - `market:CREDIT` → relevance LINKED via REFINANCING → mechanism: REFINANCING (HIGH, NEGATIVE when the company's cost of refinancing debt rises): ILLUSTRATIVE: large bond maturity next year must be refinanced → implication: RISK → **PAUSE_PURCHASES** (ADVERSE_REFINANCING_HIGH_EXPOSURE)
   - `market:EQUITY_MARKET` → relevance CONTEXT → mechanism: none applied: market strength does not justify buying and weakness does not justify selling → implication: NONE → **CONTEXT_ONLY** (BROAD_MARKET_CONTEXT)
4. **Sector / industry developments** — Technology (XLK) 3m -0.0%, vs SPY -0.5%
   - `sector:SECTOR:XLK` → relevance SECTOR_MEMBER → mechanism: none identified → implication: NONE → **CONTEXT_ONLY** (SECTOR_CONTEXT)
5. **Company-specific developments** — ZZNEW 1m -13.4%: market +0.0%, sector +0.0%, company-specific -13.4% (association, not causation)
   - `company:PRICE:2026-W40` → relevance COMPANY → mechanism: none identified (co-movement is not causation) → implication: NONE → **CONTEXT_ONLY** (COMPANY_CONTEXT)
6. **Evidence** — supporting: none cited · contradicting: none cited
7. **Valuation assumptions** — no changes proposed; stress: bear 23.19 → 20.13 (WACC +0.01, context only)
8. **Position size and next contribution** — Long-term case supports adding, but purchases are PAUSED (ADVERSE_REFINANCING_HIGH_EXPOSURE); the next contribution skips it until the pause is reassessed
9. **Missing data and freshness** — price 2026-09-30 (0 session(s) old); filings checked 0.00 h before; latest period 2026-06-30; market snapshot 2026-09-30T22:00:00.000000Z; missing: none
10. **What would change the decision** — ADD band starts at price <= 28.38 (MoS 25%) if thesis stays intact; TRIM considered at price >= 45.40 (120% of base value); EXIT considered at price >= 61.34 (bull value); EXIT if verified: Operating margin below 10% for 2 consecutive fiscal years; EXIT if verified: Loss of the largest customer contract; pause lifts when CREDIT_TIGHTENING clears, or owner re-approves the valuation/exposure with this condition considered
    - Traceability: recommendation `rec_105a8e1f08b8471c99b8`, snapshot `mks_0072e7b2e6ca4ceab314`, exposure profile `exv_4855578f322744feb653`, thesis `thv_225dd1af345c4bddbaf9`, valuation `val_e4d1ca42d9a741d5b66b`, policy `pol_90dd733085c5486e99f9`

