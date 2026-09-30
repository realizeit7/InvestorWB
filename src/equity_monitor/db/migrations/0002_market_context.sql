-- 0002_market_context: market/sector/company context, exposure profiles, observations, clusters,
-- purchase eligibility. Existing tables are extended, not replaced.

-- Point-in-time market/economic series. A revised value is a NEW row with a later public_at, so an
-- earlier as-of query keeps seeing the value that was public then.
CREATE TABLE market_series (
    series_key TEXT PRIMARY KEY,          -- e.g. fred:DGS10, finra_si:AAPL, finra_shvol:AAPL
    name TEXT NOT NULL,
    source_id TEXT NOT NULL,              -- key in market/sources.py SOURCES
    data_class TEXT NOT NULL,             -- key in market/sources.py DATA_CLASSES
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    frequency TEXT NOT NULL,              -- DAILY | WEEKLY | SEMIMONTHLY | MONTHLY
    security_id TEXT REFERENCES security(id),
    created_at TEXT NOT NULL
);

CREATE TABLE market_observation (
    id TEXT PRIMARY KEY,
    series_key TEXT NOT NULL REFERENCES market_series(series_key),
    period_date TEXT NOT NULL,
    value TEXT,                           -- NULL = provider reported the value as missing
    public_at TEXT NOT NULL,
    public_at_basis TEXT NOT NULL,        -- PROVIDED | ESTIMATED_LAG | RETRIEVAL
    vintage_basis TEXT NOT NULL,          -- FIRST_SEEN | CURRENT_VINTAGE_BACKFILL
    retrieved_at TEXT NOT NULL,
    raw_object_id TEXT REFERENCES raw_object(id),
    extra_json TEXT,
    UNIQUE (series_key, period_date, value)
);
CREATE INDEX ix_mobs ON market_observation(series_key, period_date, public_at);
CREATE TRIGGER market_observation_no_update BEFORE UPDATE ON market_observation
BEGIN SELECT RAISE(ABORT, 'market_observation is append-only; revisions are new rows'); END;

-- Shared, timestamped market-context snapshot referenced by company reviews.
CREATE TABLE market_snapshot (
    id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    session_date TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    policy_version_id TEXT REFERENCES policy_version(id),
    created_at TEXT NOT NULL,
    UNIQUE (session_date, content_hash)
);
CREATE TRIGGER market_snapshot_no_update BEFORE UPDATE ON market_snapshot
BEGIN SELECT RAISE(ABORT, 'market_snapshot is immutable'); END;
CREATE TRIGGER market_snapshot_no_delete BEFORE DELETE ON market_snapshot
BEGIN SELECT RAISE(ABORT, 'market_snapshot is immutable'); END;

-- Versioned company exposure profiles (same workflow as theses: immutable versions + approval).
CREATE TABLE exposure_profile_version (
    id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL REFERENCES security(id),
    version_no INTEGER NOT NULL,
    prev_version_id TEXT REFERENCES exposure_profile_version(id),
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    author TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'ACTUAL',
    evidence_as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (security_id, version_no)
);
CREATE TRIGGER exposure_version_no_update BEFORE UPDATE ON exposure_profile_version
BEGIN SELECT RAISE(ABORT, 'exposure_profile_version is immutable; create a new version'); END;
CREATE TABLE exposure_approval (
    id TEXT PRIMARY KEY,
    exposure_version_id TEXT NOT NULL UNIQUE REFERENCES exposure_profile_version(id),
    approved_at TEXT NOT NULL,
    approver TEXT NOT NULL,
    note TEXT
);

-- Owner-entered external research/news claims (verified against primary sources where possible).
CREATE TABLE external_observation (
    id TEXT PRIMARY KEY,
    security_id TEXT REFERENCES security(id),
    sector TEXT,
    level TEXT NOT NULL CHECK (level IN ('MARKET','SECTOR','COMPANY')),
    source_name TEXT NOT NULL,
    url TEXT,
    text TEXT NOT NULL,
    published_at TEXT NOT NULL,
    claim_id TEXT REFERENCES claim(id),
    verification TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Research tasks raised by observations whose relevance/mechanism needs judgment.
CREATE TABLE research_task (
    id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL UNIQUE,
    security_id TEXT REFERENCES security(id),
    reason TEXT NOT NULL,
    detail_json TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','DONE','DISMISSED')),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE alert_feedback (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES alert(id),
    useful INTEGER NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);

ALTER TABLE recommendation ADD COLUMN purchase_eligibility TEXT;
ALTER TABLE recommendation ADD COLUMN baseline_eligibility TEXT;
ALTER TABLE recommendation ADD COLUMN market_snapshot_id TEXT REFERENCES market_snapshot(id);
ALTER TABLE recommendation ADD COLUMN exposure_version_id TEXT REFERENCES exposure_profile_version(id);
