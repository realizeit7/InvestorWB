-- Follow-up repair: an approval covers the evidence state it was given. When a FACT claim of an approved thesis is not
-- VERIFIED (e.g. downgraded by migrations 0004/0007), the thesis may support new ADD recommendations and allocations
-- only after an explicit evidence review recorded against the claims' CURRENT statuses. Historical approvals and
-- recommendations are untouched; nothing is sold because a review is pending.
CREATE TABLE thesis_evidence_review (
    id TEXT PRIMARY KEY,
    thesis_version_id TEXT NOT NULL REFERENCES thesis_version(id),
    reviewed_at TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    note TEXT,
    claims_json TEXT NOT NULL          -- [{claim_id, verification, support_status, verifier_version}] as reviewed
);
CREATE INDEX ix_thesis_evidence_review ON thesis_evidence_review(thesis_version_id, reviewed_at);
CREATE TRIGGER thesis_evidence_review_no_update BEFORE UPDATE ON thesis_evidence_review
BEGIN SELECT RAISE(ABORT, 'thesis_evidence_review is append-only'); END;
CREATE TRIGGER thesis_evidence_review_no_delete BEFORE DELETE ON thesis_evidence_review
BEGIN SELECT RAISE(ABORT, 'thesis_evidence_review is append-only'); END;
-- Verifier ev-3 enforced direction only on CHANGE figures, so it could certify "decreased to $4 billion" against a
-- source that says revenue increased to $4 billion. Its VERIFIED claims are downgraded (support LEGACY). Re-approving
-- the thesis version (`eqm thesis approve --version-id ...`) re-verifies them under the current verifier: claims that
-- now verify need no acknowledgement; the rest need a recorded review or a corrected version.
UPDATE evidence_link SET verified = 0
 WHERE claim_id IN (SELECT id FROM claim WHERE verification = 'VERIFIED' AND verifier_version = 'ev-3');
UPDATE external_observation SET verification = 'SOURCE_MATCHED'
 WHERE claim_id IN (SELECT id FROM claim WHERE verification = 'VERIFIED' AND verifier_version = 'ev-3');
UPDATE claim SET verification = 'SOURCE_MATCHED', support_status = 'LEGACY',
       verification_detail = COALESCE(verification_detail || '; ', '') || 'downgraded: verified by ev-3 (direction on levels unchecked)'
 WHERE verification = 'VERIFIED' AND verifier_version = 'ev-3';
