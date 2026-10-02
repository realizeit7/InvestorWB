-- Company finder: discovery of possibly under-rated companies. Produces RESEARCH candidates only.
-- Every run, candidate row and LLM judgment is append-only so shortlists can be evaluated prospectively.
CREATE TABLE finder_run (
    id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    session_date TEXT NOT NULL,              -- latest completed session used for prices
    policy_version_id TEXT NOT NULL REFERENCES policy_version(id),
    params_json TEXT NOT NULL,
    universe_count INTEGER NOT NULL,
    prelim_ranked INTEGER NOT NULL,
    deep_count INTEGER NOT NULL,
    shortlist_count INTEGER NOT NULL,
    sources_json TEXT NOT NULL,              -- provider, retrieved_at, raw object ids
    warnings_json TEXT NOT NULL,
    label TEXT NOT NULL,                     -- CURRENT (never point-in-time history)
    created_at TEXT NOT NULL
);
CREATE TABLE finder_candidate (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES finder_run(id),
    stage TEXT NOT NULL CHECK (stage IN ('PRELIM','DEEP','SHORTLIST')),
    symbol TEXT NOT NULL,
    cik TEXT,
    security_id TEXT REFERENCES security(id),
    sector TEXT,
    market_cap_usd TEXT,
    price TEXT,
    price_date TEXT,
    metrics_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    score TEXT,
    rank INTEGER,
    exclusion_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, stage, symbol)
);
CREATE INDEX ix_finder_candidate_run ON finder_candidate(run_id, stage, rank);
CREATE TABLE finder_judgment (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES finder_run(id),
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,                  -- claude_code | interactive | anthropic | fixture
    llm_call_id TEXT,
    content_json TEXT NOT NULL,              -- validated judgment (opinion)
    verification_json TEXT NOT NULL,         -- per FACT claim verification
    verdict TEXT NOT NULL CHECK (verdict IN ('RESEARCH_FURTHER','LIKELY_VALUE_TRAP','INSUFFICIENT_EVIDENCE')),
    priority INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_finder_judgment_run ON finder_judgment(run_id, symbol);
CREATE TRIGGER finder_run_no_update BEFORE UPDATE ON finder_run BEGIN SELECT RAISE(ABORT, 'finder_run is append-only'); END;
CREATE TRIGGER finder_run_no_delete BEFORE DELETE ON finder_run BEGIN SELECT RAISE(ABORT, 'finder_run is append-only'); END;
CREATE TRIGGER finder_candidate_no_update BEFORE UPDATE ON finder_candidate BEGIN SELECT RAISE(ABORT, 'finder_candidate is append-only'); END;
CREATE TRIGGER finder_candidate_no_delete BEFORE DELETE ON finder_candidate BEGIN SELECT RAISE(ABORT, 'finder_candidate is append-only'); END;
CREATE TRIGGER finder_judgment_no_update BEFORE UPDATE ON finder_judgment BEGIN SELECT RAISE(ABORT, 'finder_judgment is append-only'); END;
CREATE TRIGGER finder_judgment_no_delete BEFORE DELETE ON finder_judgment BEGIN SELECT RAISE(ABORT, 'finder_judgment is append-only'); END;
