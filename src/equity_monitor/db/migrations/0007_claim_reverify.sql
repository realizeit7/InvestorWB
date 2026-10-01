-- Follow-up repair: verifier ev-2 checked each number independently and could certify swapped from/to values or a
-- comparison value presented as the current result. Claims it marked VERIFIED are downgraded to SOURCE_MATCHED
-- (support LEGACY, review required); re-create the claim to verify it under ev-3. Nothing is deleted.
UPDATE evidence_link SET verified = 0
 WHERE claim_id IN (SELECT id FROM claim WHERE verification = 'VERIFIED' AND verifier_version = 'ev-2');
UPDATE external_observation SET verification = 'SOURCE_MATCHED'
 WHERE claim_id IN (SELECT id FROM claim WHERE verification = 'VERIFIED' AND verifier_version = 'ev-2');
UPDATE claim SET verification = 'SOURCE_MATCHED', support_status = 'LEGACY',
       verification_detail = COALESCE(verification_detail || '; ', '') || 'downgraded: verified by ev-2 (relationships unchecked)'
 WHERE verification = 'VERIFIED' AND verifier_version = 'ev-2';
