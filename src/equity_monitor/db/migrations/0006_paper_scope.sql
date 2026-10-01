-- migrate: foreign_keys=off
-- Follow-up repair: a sell recommendation executes at most once PER paper portfolio (the augmented and baseline books
-- must both be able to execute it). The global UNIQUE(recommendation_id) is replaced by
-- UNIQUE(recommendation_id, paper_portfolio_id); existing execution rows are preserved with their ids.
CREATE TABLE paper_execution_new (
    id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES recommendation(id),
    paper_portfolio_id TEXT NOT NULL REFERENCES portfolio(id),
    fill_session_date TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    side TEXT NOT NULL,
    fees TEXT NOT NULL,
    slippage_bps TEXT NOT NULL,
    policy_version_id TEXT NOT NULL REFERENCES policy_version(id),
    created_at TEXT NOT NULL,
    UNIQUE (recommendation_id, paper_portfolio_id)
);
INSERT INTO paper_execution_new SELECT id, recommendation_id, paper_portfolio_id, fill_session_date, fill_price, quantity,
    side, fees, slippage_bps, policy_version_id, created_at FROM paper_execution;
DROP TABLE paper_execution;
ALTER TABLE paper_execution_new RENAME TO paper_execution;
CREATE TRIGGER paper_execution_no_update BEFORE UPDATE ON paper_execution
BEGIN SELECT RAISE(ABORT, 'paper_execution is append-only'); END;
CREATE TRIGGER paper_execution_no_delete BEFORE DELETE ON paper_execution
BEGIN SELECT RAISE(ABORT, 'paper_execution is append-only'); END;
-- One allocation execution per paper book per session: different proposals (or the other variant of the same
-- proposal) filling on the same session are mutually exclusive.
CREATE TRIGGER paper_allocation_one_per_session BEFORE INSERT ON paper_allocation_execution
WHEN EXISTS (SELECT 1 FROM paper_allocation_execution WHERE paper_portfolio_id = NEW.paper_portfolio_id
             AND fill_session_date = NEW.fill_session_date)
BEGIN SELECT RAISE(ABORT, 'another allocation proposal already executed in this paper book for this session'); END;
