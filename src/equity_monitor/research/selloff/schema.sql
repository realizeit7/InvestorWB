-- Post-Selloff Recovery Research (protocol sr-0.x). Applied ONLY to a research data home, never to the portfolio
-- database. Every table is append-only: corrections are new rows; the latest row per key wins where stated.
CREATE TABLE IF NOT EXISTS research_home (
    kind TEXT PRIMARY KEY CHECK (kind = 'SELLOFF_RESEARCH'),
    created_at TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_protocol (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    content_yaml TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_search (
    id TEXT PRIMARY KEY,
    protocol_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('MAIN','SENSITIVITY')),
    phrase TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    page_from INTEGER NOT NULL,
    total_reported INTEGER,
    total_relation TEXT,
    returned INTEGER NOT NULL,
    raw_object_id TEXT,
    error TEXT,
    executed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_hit (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES sr_search(id),
    accession TEXT NOT NULL,
    file_name TEXT,
    cik TEXT NOT NULL,
    form TEXT,
    file_date TEXT,
    display_name TEXT,                 -- EDGAR shows TODAY's name/ticker: never used as the historical ticker
    items TEXT,
    sics TEXT,
    UNIQUE (search_id, accession, file_name)
);
CREATE INDEX IF NOT EXISTS ix_sr_hit_acc ON sr_hit(accession);
CREATE TABLE IF NOT EXISTS sr_filing (     -- one row per screened accession: where its documents and timing came from
    accession TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    issuer_id TEXT REFERENCES issuer(id),
    form TEXT,
    accepted_at TEXT,                  -- EDGAR acceptance (UTC, exact) = document submitted
    accepted_basis TEXT,
    filing_date TEXT,
    main_document_id TEXT REFERENCES source_document(id),
    hit_document_id TEXT REFERENCES source_document(id),
    cover_json TEXT NOT NULL,          -- company name, trading symbols and exchanges as stated on the 8-K cover page
    dateline_date TEXT,                -- release dateline (date-only), when present
    dateline_quote TEXT,
    retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_screen (
    id TEXT PRIMARY KEY,
    accession TEXT NOT NULL,
    sample_rank INTEGER NOT NULL,
    sample_key TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('INCLUDED','EXCLUDED','DUPLICATE','UNRESOLVED')),
    category TEXT NOT NULL CHECK (category IN ('PRIMARY_ENDPOINT_FAILURE','FUTILITY_STOP','SAFETY_STOP',
        'COMMERCIAL_DISCONTINUATION','REGULATORY_REJECTION','MIXED_OR_UNCLEAR','NOT_AN_EVENT','OUT_OF_SCOPE')),
    phase TEXT,
    reason TEXT NOT NULL,
    quote TEXT,
    quote_passage_id TEXT,
    quote_verified INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL,        -- drug, indication, trial id, partner flag, flags
    screener TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sr_screen_acc ON sr_screen(accession, created_at);
CREATE TABLE IF NOT EXISTS sr_event (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    cik TEXT NOT NULL,
    issuer_id TEXT REFERENCES issuer(id),
    security_id TEXT REFERENCES security(id),
    company_name_at_time TEXT,
    ticker_at_time TEXT,
    exchange_at_time TEXT,
    security_class TEXT,
    drug TEXT, indication TEXT, trial_id TEXT, trial_id_source TEXT,
    phase TEXT NOT NULL,
    category TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    occurred_date TEXT, occurred_basis TEXT,
    submitted_at TEXT,                 -- EDGAR acceptance of the earliest filing (exact)
    public_earliest TEXT NOT NULL,     -- earliest possible public time
    public_latest TEXT NOT NULL,       -- latest possible public time (first exact verified timestamp)
    public_precision TEXT NOT NULL CHECK (public_precision IN ('EXACT','DATE_ONLY')),
    public_basis TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,        -- when this system retrieved the source
    pre_session TEXT NOT NULL,         -- last session closing before public_earliest
    measurement_session TEXT NOT NULL, -- first session closing after public_latest
    decision_cutoff TEXT NOT NULL,
    entry_session TEXT NOT NULL,       -- first session opening after the cutoff
    timing_notes TEXT,
    accessions_json TEXT NOT NULL,     -- all filings merged into this event (dedup)
    repeat_of TEXT,                    -- earlier event of the same drug in a different trial
    screen_id TEXT NOT NULL REFERENCES sr_screen(id),
    protocol_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sr_event_key ON sr_event(event_key);
CREATE TABLE IF NOT EXISTS sr_source (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES sr_event(id),
    document_id TEXT NOT NULL REFERENCES source_document(id),
    role TEXT NOT NULL,                -- EVENT_FILING | EVENT_EXHIBIT | LATEST_10Q | LATEST_10K | EARLIER_FILING
    public_at TEXT,
    excluded_reason TEXT,              -- set when a known document was NOT usable at the cutoff
    created_at TEXT NOT NULL,
    UNIQUE (event_id, document_id, role)
);
CREATE TABLE IF NOT EXISTS sr_gap (
    id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES sr_event(id),
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_fact (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES sr_event(id),
    section TEXT NOT NULL CHECK (section IN ('A_FAILURE','B_REMAINING_BUSINESS','C_FINANCING','D_RECOVERY')),
    field TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('FACT','ASSUMPTION','OPINION')),
    value_text TEXT,
    value_num TEXT,
    unit TEXT,
    period TEXT,
    null_reason TEXT,                  -- missing values: None + reason, never zero
    source_kind TEXT NOT NULL CHECK (source_kind IN ('XBRL','PASSAGE','DERIVED','NONE')),
    fact_ref TEXT,                     -- financial_fact id(s)
    passage_id TEXT,
    quote TEXT,
    verification_status TEXT NOT NULL,
    verifier_version TEXT,
    author TEXT NOT NULL,              -- DETERMINISTIC | HUMAN | LLM_RETROSPECTIVE
    label TEXT,                        -- e.g. RETROSPECTIVE_CONTAMINATED, LATER_REVISED
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sr_fact_event ON sr_fact(event_id, section);
CREATE TABLE IF NOT EXISTS sr_price_check (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,             -- event id or benchmark symbol
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,              -- OK | NO_DATA | IDENTITY_UNVERIFIED | ERROR
    detail_json TEXT NOT NULL,
    raw_object_id TEXT,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_eligibility (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES sr_event(id),
    status TEXT NOT NULL CHECK (status IN ('ELIGIBLE','INELIGIBLE','UNDETERMINED')),
    detail_json TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_outcome_audit (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES sr_event(id),
    detail_json TEXT NOT NULL,         -- availability only: no returns are computed in sr-0.1
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_judgment (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES sr_event(id),
    provider TEXT NOT NULL,
    pack_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label = 'RETROSPECTIVE_CONTAMINATED'),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_effort (
    id TEXT PRIMARY KEY,
    event_id TEXT,
    step TEXT NOT NULL,
    seconds REAL NOT NULL,
    automated INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
