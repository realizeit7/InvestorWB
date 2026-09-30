-- Repair: debt totals and components are separate concepts (normalizer norm-2).
-- norm-1 folded several XBRL tags into two concepts, keeping only the first tag found. Rows are remapped by
-- their source tag so nothing is relabelled as a different quantity. Components norm-1 never stored stay
-- absent (unknown) until the issuer is re-synced; they are never assumed to be zero.
UPDATE financial_fact SET concept = 'long_term_debt_noncurrent'
 WHERE concept = 'long_term_debt' AND source_tag = 'LongTermDebtNoncurrent';
UPDATE financial_fact SET concept = 'long_term_debt_total'
 WHERE concept = 'long_term_debt' AND source_tag = 'LongTermDebt';
UPDATE financial_fact SET concept = 'long_term_debt_current'
 WHERE concept = 'current_debt' AND source_tag = 'LongTermDebtCurrent';
UPDATE financial_fact SET concept = 'debt_current'
 WHERE concept = 'current_debt' AND source_tag = 'DebtCurrent';
UPDATE financial_fact SET concept = 'short_term_borrowings'
 WHERE concept = 'current_debt' AND source_tag = 'ShortTermBorrowings';
