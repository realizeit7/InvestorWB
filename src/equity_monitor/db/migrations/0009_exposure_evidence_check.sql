-- Exposure-profile evidence is re-checked under the current claim verifier. Profile versions and approvals are
-- immutable and preserved; each re-check is a new row here. An approved profile supports purchase eligibility only
-- when its evidence, checked by the CURRENT verifier at or before the decision, has no FAILED item and every
-- unresolved (SOURCE_MATCHED/UNVERIFIED) item was explicitly acknowledged against that same status.
CREATE TABLE exposure_evidence_check (
    id TEXT PRIMARY KEY,
    exposure_version_id TEXT NOT NULL REFERENCES exposure_profile_version(id),
    checked_at TEXT NOT NULL,
    verifier_version TEXT NOT NULL,
    verification_json TEXT NOT NULL,      -- [{factor, status, details}] for EVIDENCED exposures, as re-checked
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0,1)),
    reviewer TEXT,
    note TEXT
);
CREATE INDEX ix_exposure_evidence_check ON exposure_evidence_check(exposure_version_id, checked_at);
CREATE TRIGGER exposure_evidence_check_no_update BEFORE UPDATE ON exposure_evidence_check
BEGIN SELECT RAISE(ABORT, 'exposure_evidence_check is append-only'); END;
CREATE TRIGGER exposure_evidence_check_no_delete BEFORE DELETE ON exposure_evidence_check
BEGIN SELECT RAISE(ABORT, 'exposure_evidence_check is append-only'); END;
