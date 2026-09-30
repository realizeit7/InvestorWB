-- migrate: foreign_keys=off
-- Repair: citation integrity (SOURCE_MATCHED) is recorded separately from substantive support. SQLite cannot alter
-- the verification CHECK, so the claim table is rebuilt; existing rows keep their ids. Rows verified by the old
-- quote/number matcher are downgraded to SOURCE_MATCHED: that matcher proved only that the citation was intact.
CREATE TABLE claim_new (
    id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,         -- THESIS_VERSION | LLM_CALL | EXTERNAL
    owner_id TEXT NOT NULL,
    text TEXT NOT NULL,
    claim_type TEXT NOT NULL CHECK (claim_type IN ('FACT','ASSUMPTION','OPINION')),
    verification TEXT NOT NULL CHECK (verification IN ('VERIFIED','SOURCE_MATCHED','UNVERIFIED','FAILED','NOT_REQUIRED')),
    verification_detail TEXT,
    citation_status TEXT CHECK (citation_status IN ('SOURCE_MATCHED','FAILED')),
    support_status TEXT CHECK (support_status IN ('CONFIRMED','CONTRADICTED','UNSUPPORTED','PARTIAL','NOT_CHECKABLE','LEGACY')),
    verifier_version TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO claim_new (id, owner_type, owner_id, text, claim_type, verification, verification_detail, citation_status,
                       support_status, verifier_version, created_at)
SELECT id, owner_type, owner_id, text, claim_type,
       CASE WHEN verification = 'VERIFIED' THEN 'SOURCE_MATCHED' ELSE verification END,
       verification_detail,
       CASE WHEN verification = 'VERIFIED' THEN 'SOURCE_MATCHED' WHEN verification = 'FAILED' THEN 'FAILED' END,
       CASE WHEN verification = 'VERIFIED' THEN 'LEGACY' END,
       'legacy-1', created_at
FROM claim;
DROP TABLE claim;
ALTER TABLE claim_new RENAME TO claim;
UPDATE evidence_link SET verified = 0 WHERE verified = 1;
UPDATE external_observation SET verification = 'SOURCE_MATCHED' WHERE verification = 'VERIFIED';
