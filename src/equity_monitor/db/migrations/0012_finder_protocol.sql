-- Finder evaluation protocol (review of 77a3ad1): frozen cohorts per comparison arm, Claude Code provider checks,
-- atomic subscription call slots.
CREATE TABLE finder_cohort (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES finder_run(id),
    arm TEXT NOT NULL CHECK (arm IN ('A_QUALITY_VALUE','B_RAW_GAP','C_CONSERVATIVE_GAP','D_LLM_RULE')),
    cohort_no INTEGER NOT NULL,              -- D: a later judgment freeze creates a NEW cohort, never edits one
    protocol_hash TEXT NOT NULL,             -- hash of the finder rules in force when the run was made
    rule_json TEXT NOT NULL,                 -- the arm's selection rule (weights / LLM rule) as applied
    info_time TEXT NOT NULL,                 -- when every input to this selection existed (entry is after this)
    members_json TEXT NOT NULL,              -- [{symbol, security_id, rank, score, judgment_id?}]
    excluded_json TEXT NOT NULL,             -- D: shortlist names left out and why (not judged, rule not met)
    created_at TEXT NOT NULL,
    UNIQUE (run_id, arm, cohort_no)
);
CREATE INDEX ix_finder_cohort_run ON finder_cohort(run_id, arm);
CREATE TRIGGER finder_cohort_no_update BEFORE UPDATE ON finder_cohort BEGIN SELECT RAISE(ABORT, 'finder_cohort is append-only'); END;
CREATE TRIGGER finder_cohort_no_delete BEFORE DELETE ON finder_cohort BEGIN SELECT RAISE(ABORT, 'finder_cohort is append-only'); END;

CREATE TABLE llm_provider_check (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    cli_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('PASS','FAIL')),
    live INTEGER NOT NULL DEFAULT 0,         -- 1 when an owner-authorized live call was part of the check
    details_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TRIGGER llm_provider_check_no_update BEFORE UPDATE ON llm_provider_check BEGIN SELECT RAISE(ABORT, 'llm_provider_check is append-only'); END;
CREATE TRIGGER llm_provider_check_no_delete BEFORE DELETE ON llm_provider_check BEGIN SELECT RAISE(ABORT, 'llm_provider_check is append-only'); END;

-- subscription providers reserve a call slot (amount 0) before launching; counted per provider and UTC day
ALTER TABLE llm_budget_entry ADD COLUMN provider TEXT;
