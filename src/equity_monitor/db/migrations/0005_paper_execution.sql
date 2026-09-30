-- Repair: paper allocation fills are atomic and idempotent, with provenance to the originating proposal,
-- recommendations and FROZEN policy. One execution per (proposal, variant, paper portfolio).
CREATE TABLE paper_allocation_execution (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES allocation_proposal(id),
    variant TEXT NOT NULL CHECK (variant IN ('augmented','baseline')),
    paper_portfolio_id TEXT NOT NULL REFERENCES portfolio(id),
    fill_session_date TEXT NOT NULL,
    policy_version_id TEXT NOT NULL REFERENCES policy_version(id),
    fills_json TEXT NOT NULL,          -- per line: symbol, recommendation_id, qty, price, fee, amount
    skipped_json TEXT NOT NULL,        -- per line: symbol, reason (eligibility, cash, limits, rounding)
    ledger_event_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (proposal_id, variant, paper_portfolio_id)
);
CREATE TRIGGER paper_allocation_execution_no_update BEFORE UPDATE ON paper_allocation_execution
BEGIN SELECT RAISE(ABORT, 'paper_allocation_execution is append-only'); END;
CREATE TRIGGER paper_allocation_execution_no_delete BEFORE DELETE ON paper_allocation_execution
BEGIN SELECT RAISE(ABORT, 'paper_allocation_execution is append-only'); END;
