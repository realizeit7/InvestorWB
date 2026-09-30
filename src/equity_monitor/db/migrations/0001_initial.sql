-- 0001_initial: core schema for equity_monitor.
-- Conventions: TEXT ids; UTC ISO-8601 timestamps ending in 'Z'; decimals stored as TEXT;
-- NULL means unknown (never an implicit zero). Evidence/decision tables are append-only
-- (enforced by triggers below); corrections are new rows that reference the old ones.

-- ============================================================ audit
CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,              -- 'user' | 'system' | 'job:<name>' | 'test'
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT,
    detail_json TEXT
);

-- ============================================================ securities
CREATE TABLE issuer (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cik TEXT UNIQUE,                  -- 10-digit zero padded when known
    sic TEXT,
    sic_description TEXT,
    sector TEXT,                      -- coarse sector used for concentration limits
    industry_group TEXT,              -- peer group for screening comparisons
    fiscal_year_end TEXT,             -- MMDD from SEC submissions when known
    created_at TEXT NOT NULL
);

CREATE TABLE security (
    id TEXT PRIMARY KEY,
    issuer_id TEXT REFERENCES issuer(id),
    symbol TEXT NOT NULL,             -- current display ticker (history in security_identifier)
    share_class TEXT,
    security_type TEXT NOT NULL CHECK (security_type IN
        ('COMMON','ETF','ADR','PREFERRED','FUND','OTHER','UNKNOWN')),
    exchange TEXT,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL
);

CREATE TABLE security_identifier (
    security_id TEXT NOT NULL REFERENCES security(id),
    id_type TEXT NOT NULL CHECK (id_type IN ('TICKER','CIK','CUSIP','FIGI','ISIN')),
    value TEXT NOT NULL,
    valid_from TEXT,                  -- date; NULL = since before tracking
    valid_to TEXT,                    -- date; NULL = still valid
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (security_id, id_type, value, valid_from)
);
CREATE INDEX ix_secid_value ON security_identifier(id_type, value);

CREATE TABLE universe_snapshot (
    id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    source TEXT NOT NULL,
    method TEXT NOT NULL,
    point_in_time INTEGER NOT NULL,   -- 0 => ENGINEERING_ONLY for historical claims
    created_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE universe_member (
    snapshot_id TEXT NOT NULL REFERENCES universe_snapshot(id),
    security_id TEXT NOT NULL REFERENCES security(id),
    included INTEGER NOT NULL,
    exclusion_reason TEXT,
    PRIMARY KEY (snapshot_id, security_id)
);

-- ============================================================ portfolios & ledger
CREATE TABLE portfolio (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('ACTUAL','PAPER','FIXTURE','HYPOTHETICAL')),
    base_currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE account (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolio(id),
    name TEXT NOT NULL,
    broker TEXT,
    tax_status TEXT NOT NULL DEFAULT 'UNKNOWN',   -- TAXABLE | TAX_DEFERRED | TAX_FREE | UNKNOWN
    settlement_days INTEGER NOT NULL DEFAULT 1,   -- US equities T+1 since 2024-05-28
    created_at TEXT NOT NULL,
    UNIQUE (portfolio_id, name)
);

CREATE TABLE import_batch (
    id TEXT PRIMARY KEY,
    account_id TEXT REFERENCES account(id),
    kind TEXT NOT NULL,               -- 'transactions' | 'snapshot' | 'prices'
    source_path TEXT,
    file_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    rows_total INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    rows_duplicate INTEGER NOT NULL,
    rows_rejected INTEGER NOT NULL,
    detail_json TEXT
);

CREATE TABLE ledger_event (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,      -- global insertion order (tie-breaker for same-day replay)
    account_id TEXT NOT NULL REFERENCES account(id),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'DEPOSIT','WITHDRAWAL','BUY','SELL','FEE','DIVIDEND','INTEREST','SPLIT',
        'OPENING_POSITION','OPENING_CASH','REVERSAL','CORPORATE_ACTION')),
    security_id TEXT REFERENCES security(id),
    trade_date TEXT NOT NULL,         -- effective date (ex-date for splits)
    settle_date TEXT,
    occurred_at TEXT,                 -- UTC timestamp when known
    source_timezone TEXT,             -- timezone the source reported in
    quantity TEXT,                    -- shares (positive); SPLIT uses ratio columns
    price TEXT,
    fees TEXT,                        -- always >= 0; charged to cash
    amount TEXT,                      -- cash amount for DEPOSIT/WITHDRAWAL/DIVIDEND/FEE/INTEREST/OPENING_CASH
    currency TEXT NOT NULL DEFAULT 'USD',
    cost_basis_total TEXT,            -- OPENING_POSITION: NULL = unknown basis
    ratio_num TEXT,                   -- SPLIT new shares per ...
    ratio_den TEXT,                   -- ... old shares
    action_subtype TEXT,              -- CORPORATE_ACTION: MERGER | SPINOFF | ...
    link_group_id TEXT,               -- e.g. dividend + reinvestment purchase
    reverses_event_id TEXT REFERENCES ledger_event(id),
    replaces_event_id TEXT REFERENCES ledger_event(id),
    external_id TEXT,
    import_batch_id TEXT REFERENCES import_batch(id),
    import_row INTEGER,
    dedupe_key TEXT NOT NULL UNIQUE,
    note TEXT,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL
);
CREATE INDEX ix_ledger_account_date ON ledger_event(account_id, trade_date, seq);

CREATE TRIGGER ledger_event_no_update BEFORE UPDATE ON ledger_event
BEGIN SELECT RAISE(ABORT, 'ledger_event is append-only; record a REVERSAL instead'); END;
CREATE TRIGGER ledger_event_no_delete BEFORE DELETE ON ledger_event
BEGIN SELECT RAISE(ABORT, 'ledger_event is append-only; record a REVERSAL instead'); END;

CREATE TABLE brokerage_snapshot (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES account(id),
    as_of_date TEXT NOT NULL,
    source TEXT,
    file_sha256 TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE (account_id, as_of_date, file_sha256)
);
CREATE TABLE brokerage_snapshot_line (
    snapshot_id TEXT NOT NULL REFERENCES brokerage_snapshot(id),
    line_type TEXT NOT NULL CHECK (line_type IN ('POSITION','CASH')),
    security_id TEXT REFERENCES security(id),
    quantity TEXT,
    cash TEXT,
    market_value TEXT,
    cost_basis_total TEXT
);

CREATE TABLE reconciliation_issue (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    account_id TEXT REFERENCES account(id),
    security_id TEXT REFERENCES security(id),
    issue_type TEXT NOT NULL,         -- UNSUPPORTED_CORPORATE_ACTION | SNAPSHOT_MISMATCH | MISSING_SPLIT | NEGATIVE_CASH | ...
    severity TEXT NOT NULL CHECK (severity IN ('CRITICAL','WARNING','INFO')),
    detail_json TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED','ACKNOWLEDGED')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);

-- ============================================================ provenance & raw data
CREATE TABLE raw_object (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    url TEXT,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,               -- relative to the raw store root; written once, never overwritten
    content_type TEXT,
    bytes INTEGER NOT NULL,
    retrieved_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE source_document (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,           -- SEC_EDGAR | COMPANY_IR | NEWS | USER | FIXTURE
    doc_type TEXT NOT NULL,           -- 10-K | 10-Q | 8-K | 10-K/A | IR_RELEASE | NEWS | NOTE
    issuer_id TEXT REFERENCES issuer(id),
    accession_no TEXT,
    source_url TEXT,
    title TEXT,
    fiscal_period_end TEXT,           -- (1) period end
    filed_date TEXT,
    public_at TEXT,                   -- (2) when the information became public (UTC); NULL = unknown
    public_at_basis TEXT,             -- ACCEPTANCE_TIME | FILED_DATE_END_OF_DAY | PROVIDED | UNKNOWN
    retrieved_at TEXT NOT NULL,       -- (3) when we retrieved it
    raw_object_id TEXT REFERENCES raw_object(id),
    content_hash TEXT,
    parser_version TEXT,
    trust TEXT NOT NULL CHECK (trust IN ('PRIMARY','SECONDARY','FIXTURE','USER')),
    items TEXT,                       -- 8-K item numbers, comma separated
    limitations TEXT,
    UNIQUE (provider, accession_no, doc_type)
);

CREATE TABLE document_passage (
    id TEXT PRIMARY KEY,              -- <document_id>#p<n>
    document_id TEXT NOT NULL REFERENCES source_document(id),
    ordinal INTEGER NOT NULL,
    section TEXT,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL
);

CREATE TABLE financial_fact (
    id TEXT PRIMARY KEY,
    issuer_id TEXT NOT NULL REFERENCES issuer(id),
    concept TEXT NOT NULL,            -- canonical concept, e.g. revenue, operating_income
    source_taxonomy TEXT,
    source_tag TEXT,
    unit TEXT NOT NULL,               -- USD | shares | USD/shares | pure
    value TEXT,                       -- NULL => missing, see null_reason
    null_reason TEXT,
    period_type TEXT NOT NULL CHECK (period_type IN ('DURATION','INSTANT')),
    period_start TEXT,
    period_end TEXT NOT NULL,
    duration_months INTEGER,          -- 3, 6, 9, 12 for durations
    fiscal_year INTEGER,
    fiscal_period TEXT,               -- FY | Q1..Q4 (as filed)
    derived INTEGER NOT NULL DEFAULT 0,
    derivation_note TEXT,
    form TEXT,
    accession_no TEXT,
    filed_date TEXT,
    public_at TEXT NOT NULL,          -- availability for point-in-time queries
    document_id TEXT REFERENCES source_document(id),
    raw_object_id TEXT REFERENCES raw_object(id),
    normalizer_version TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (issuer_id, concept, period_start, period_end, accession_no, derived)
);
CREATE INDEX ix_fact_lookup ON financial_fact(issuer_id, concept, period_end, public_at);

CREATE TABLE price_bar (
    security_id TEXT NOT NULL REFERENCES security(id),
    session_date TEXT NOT NULL,
    open TEXT, high TEXT, low TEXT,
    close TEXT NOT NULL,              -- raw (unadjusted) close
    volume TEXT,
    provider TEXT NOT NULL,
    raw_object_id TEXT REFERENCES raw_object(id),
    retrieved_at TEXT NOT NULL,
    adjustment_note TEXT,
    PRIMARY KEY (security_id, session_date, provider)
);

CREATE TABLE corporate_action (
    id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL REFERENCES security(id),
    action_type TEXT NOT NULL CHECK (action_type IN
        ('SPLIT','CASH_DIVIDEND','MERGER','SPINOFF','SYMBOL_CHANGE','DELISTING','OTHER')),
    ex_date TEXT NOT NULL,
    pay_date TEXT,
    ratio_num TEXT,
    ratio_den TEXT,
    cash_amount TEXT,
    provider TEXT NOT NULL,
    raw_object_id TEXT REFERENCES raw_object(id),
    retrieved_at TEXT NOT NULL,
    UNIQUE (security_id, action_type, ex_date, provider)
);

CREATE TABLE data_quality_issue (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,              -- SECURITY | ISSUER | PROVIDER | PORTFOLIO
    ref_id TEXT,
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('CRITICAL','WARNING','INFO')),
    detail TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED'))
);

CREATE TABLE source_check (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,            -- issuer id / security id / 'universe'
    check_type TEXT NOT NULL,         -- FILINGS | PRICES | FACTS
    checked_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    latest_seen TEXT,                 -- latest accession / price date observed
    error TEXT,
    job_run_id TEXT
);
CREATE INDEX ix_source_check ON source_check(subject, check_type, checked_at);

-- ============================================================ research & thesis
CREATE TABLE llm_call (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    inputs_json TEXT,                 -- redacted inputs (document ids, passages refs); no account data
    response_text TEXT,
    parsed_json TEXT,
    validation_json TEXT,
    status TEXT NOT NULL,             -- OK | INVALID | ERROR | REFUSED
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE thesis (
    id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL UNIQUE REFERENCES security(id),
    created_at TEXT NOT NULL
);

CREATE TABLE thesis_version (
    id TEXT PRIMARY KEY,
    thesis_id TEXT NOT NULL REFERENCES thesis(id),
    version_no INTEGER NOT NULL,
    prev_version_id TEXT REFERENCES thesis_version(id),
    content_json TEXT NOT NULL,       -- structured thesis fields (see research/thesis.py)
    content_hash TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    evidence_as_of TEXT NOT NULL,     -- latest public_at of evidence available when written
    evidence_json TEXT,               -- ids of documents/facts available at the time
    author TEXT NOT NULL,             -- USER | LLM_DRAFT | FIXTURE
    llm_call_id TEXT REFERENCES llm_call(id),
    label TEXT NOT NULL DEFAULT 'ACTUAL',
    created_at TEXT NOT NULL,
    UNIQUE (thesis_id, version_no)
);
CREATE TRIGGER thesis_version_no_update BEFORE UPDATE ON thesis_version
BEGIN SELECT RAISE(ABORT, 'thesis_version is immutable; create a new version'); END;
CREATE TRIGGER thesis_version_no_delete BEFORE DELETE ON thesis_version
BEGIN SELECT RAISE(ABORT, 'thesis_version is immutable'); END;

CREATE TABLE thesis_approval (
    id TEXT PRIMARY KEY,
    thesis_version_id TEXT NOT NULL UNIQUE REFERENCES thesis_version(id),
    approved_at TEXT NOT NULL,
    approver TEXT NOT NULL,
    note TEXT
);

CREATE TABLE claim (
    id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,         -- THESIS_VERSION | LLM_CALL
    owner_id TEXT NOT NULL,
    text TEXT NOT NULL,
    claim_type TEXT NOT NULL CHECK (claim_type IN ('FACT','ASSUMPTION','OPINION')),
    verification TEXT NOT NULL CHECK (verification IN ('VERIFIED','UNVERIFIED','FAILED','NOT_REQUIRED')),
    verification_detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE evidence_link (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claim(id),
    document_id TEXT REFERENCES source_document(id),
    passage_id TEXT REFERENCES document_passage(id),
    fact_id TEXT REFERENCES financial_fact(id),
    quote TEXT,
    supports INTEGER NOT NULL DEFAULT 1,   -- 1 supporting, 0 contradicting
    verified INTEGER NOT NULL
);

CREATE TABLE milestone (
    id TEXT PRIMARY KEY,
    thesis_version_id TEXT NOT NULL REFERENCES thesis_version(id),
    description TEXT NOT NULL,
    concept TEXT,
    comparator TEXT,                  -- '>=' '<=' '>' '<'
    target TEXT,
    due_date TEXT
);
CREATE TABLE invalidation_condition (
    id TEXT PRIMARY KEY,
    thesis_version_id TEXT NOT NULL REFERENCES thesis_version(id),
    description TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('METRIC','EVENT','JUDGMENT')),
    concept TEXT,
    comparator TEXT,
    threshold TEXT,
    consecutive_periods INTEGER,
    period_basis TEXT                 -- FY | TTM | Q
);
CREATE TABLE condition_assessment (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('MILESTONE','INVALIDATION')),
    subject_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('NOT_TRIGGERED','TRIGGERED','AMBIGUOUS','UNKNOWN','MET','MISSED','PENDING')),
    verified INTEGER NOT NULL,
    assessor TEXT NOT NULL,           -- ENGINE | USER | LLM
    evidence_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE watchlist_entry (
    security_id TEXT PRIMARY KEY REFERENCES security(id),
    status TEXT NOT NULL CHECK (status IN ('RESEARCH','APPROVED','REJECTED','ARCHIVED')),
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE screening_run (
    id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    universe_snapshot_id TEXT REFERENCES universe_snapshot(id),
    config_hash TEXT NOT NULL,
    label TEXT NOT NULL,              -- CURRENT | ENGINEERING_ONLY | FIXTURE
    created_at TEXT NOT NULL
);
CREATE TABLE screening_result (
    run_id TEXT NOT NULL REFERENCES screening_run(id),
    security_id TEXT NOT NULL REFERENCES security(id),
    eligible INTEGER NOT NULL,
    exclusion_reason TEXT,
    metrics_json TEXT NOT NULL,
    score TEXT,                       -- NULL when insufficient data
    completeness TEXT,
    peer_group TEXT,
    rank INTEGER,
    PRIMARY KEY (run_id, security_id)
);

-- ============================================================ valuation & policy
CREATE TABLE valuation_version (
    id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL REFERENCES security(id),
    version_no INTEGER NOT NULL,
    prev_version_id TEXT REFERENCES valuation_version(id),
    model TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    evidence_as_of TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    bear_value_ps TEXT, base_value_ps TEXT, bull_value_ps TEXT,
    author TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'ACTUAL',
    change_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (security_id, version_no)
);
CREATE TRIGGER valuation_version_no_update BEFORE UPDATE ON valuation_version
BEGIN SELECT RAISE(ABORT, 'valuation_version is immutable; create a new version'); END;
CREATE TRIGGER valuation_version_no_delete BEFORE DELETE ON valuation_version
BEGIN SELECT RAISE(ABORT, 'valuation_version is immutable'); END;

CREATE TABLE valuation_approval (
    id TEXT PRIMARY KEY,
    valuation_version_id TEXT NOT NULL UNIQUE REFERENCES valuation_version(id),
    approved_at TEXT NOT NULL,
    approver TEXT NOT NULL,
    downside_reviewed INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE policy_version (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('PREVIEW','APPROVED','FROZEN')),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approver TEXT
);

-- ============================================================ recommendations & decisions
CREATE TABLE recommendation (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolio(id),
    security_id TEXT NOT NULL REFERENCES security(id),
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    business_assessment TEXT NOT NULL CHECK (business_assessment IN ('INTACT','WEAKENING','BROKEN','UNKNOWN')),
    action TEXT NOT NULL CHECK (action IN ('ADD','HOLD','TRIM','EXIT','REVIEW')),
    previous_action TEXT,
    previous_recommendation_id TEXT REFERENCES recommendation(id),
    reason_codes_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    payload_json TEXT NOT NULL,       -- changes, evidence, freshness, downside, concentration, missing, next review, change conditions
    thesis_version_id TEXT REFERENCES thesis_version(id),
    valuation_version_id TEXT REFERENCES valuation_version(id),
    policy_version_id TEXT NOT NULL REFERENCES policy_version(id),
    input_hash TEXT NOT NULL,
    is_preview INTEGER NOT NULL,
    label TEXT NOT NULL               -- ACTUAL | PAPER | FIXTURE | ILLUSTRATIVE | HYPOTHETICAL
);
CREATE INDEX ix_rec_sec ON recommendation(portfolio_id, security_id, as_of);
CREATE TRIGGER recommendation_no_update BEFORE UPDATE ON recommendation
BEGIN SELECT RAISE(ABORT, 'recommendation is immutable'); END;
CREATE TRIGGER recommendation_no_delete BEFORE DELETE ON recommendation
BEGIN SELECT RAISE(ABORT, 'recommendation is immutable'); END;

CREATE TABLE allocation_proposal (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolio(id),
    as_of TEXT NOT NULL,
    contribution_kind TEXT NOT NULL CHECK (contribution_kind IN ('CONFIRMED','HYPOTHETICAL')),
    budget TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    remaining_cash TEXT NOT NULL,
    policy_version_id TEXT NOT NULL REFERENCES policy_version(id),
    is_preview INTEGER NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER allocation_no_update BEFORE UPDATE ON allocation_proposal
BEGIN SELECT RAISE(ABORT, 'allocation_proposal is immutable'); END;

CREATE TABLE user_decision (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('RECOMMENDATION','ALLOCATION','THESIS','VALUATION','POLICY')),
    subject_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPT','REJECT','OVERRIDE','DEFER')),
    override_action TEXT,
    rationale TEXT,
    decided_at TEXT NOT NULL,
    decided_by TEXT NOT NULL
);
CREATE TRIGGER user_decision_no_update BEFORE UPDATE ON user_decision
BEGIN SELECT RAISE(ABORT, 'user_decision is immutable'); END;

-- ============================================================ monitoring
CREATE TABLE job_run (
    id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING','SUCCESS','PARTIAL','FAILED','SKIPPED')),
    attempt INTEGER NOT NULL DEFAULT 1,
    detail_json TEXT,
    error TEXT
);

CREATE TABLE detected_event (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,   -- deterministic; re-detection is a no-op
    event_type TEXT NOT NULL,
    security_id TEXT REFERENCES security(id),
    issuer_id TEXT REFERENCES issuer(id),
    severity TEXT NOT NULL CHECK (severity IN ('CRITICAL','MATERIAL','INFO')),
    verified INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    public_at TEXT,
    document_id TEXT REFERENCES source_document(id),
    job_run_id TEXT REFERENCES job_run(id),
    payload_json TEXT
);

CREATE TABLE alert (
    id TEXT PRIMARY KEY,
    alert_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,               -- MATERIAL_EVENT | WEEKLY_DIGEST | MONTHLY_ALLOCATION | HEALTH
    event_id TEXT REFERENCES detected_event(id),
    security_id TEXT REFERENCES security(id),
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW','ACKNOWLEDGED','SNOOZED')),
    snoozed_until TEXT
);
CREATE TABLE alert_status_log (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES alert(id),
    at TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT
);

CREATE TABLE delivery_outbox (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES alert(id),
    channel TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('PENDING','SENT','FAILED','AMBIGUOUS','DEAD','HELD')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (alert_id, channel)
);
CREATE TABLE delivery_attempt (
    id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(id),
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL,            -- SENT | RETRYABLE_ERROR | PERMANENT_ERROR | AMBIGUOUS_TIMEOUT
    http_status INTEGER,
    error TEXT
);

-- ============================================================ evaluation & costs
CREATE TABLE benchmark_snapshot (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolio(id),
    benchmark_symbol TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    units TEXT NOT NULL,
    value TEXT NOT NULL,
    method_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (portfolio_id, benchmark_symbol, as_of_date)
);

CREATE TABLE paper_execution (
    id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL UNIQUE REFERENCES recommendation(id),
    paper_portfolio_id TEXT NOT NULL REFERENCES portfolio(id),
    fill_session_date TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    side TEXT NOT NULL,
    fees TEXT NOT NULL,
    slippage_bps TEXT NOT NULL,
    policy_version_id TEXT NOT NULL REFERENCES policy_version(id),
    created_at TEXT NOT NULL
);

CREATE TABLE evaluation_result (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    portfolio_id TEXT REFERENCES portfolio(id),
    as_of TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE cost_record (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL CHECK (category IN ('LLM','DATA','OTHER')),
    provider TEXT NOT NULL,
    amount_usd TEXT,                  -- NULL = unknown cost
    estimated INTEGER NOT NULL,
    units_json TEXT,
    ref_id TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE setting (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
