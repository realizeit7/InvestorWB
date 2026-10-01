-- Conservative LLM spending control. Every paid request first RESERVES a worst-case amount; the reservation is
-- later SETTLED to the actual cost (or kept in full when usage/pricing is unknown). Committed spend for a month =
-- settled actuals + open reservations. Append-only.
CREATE TABLE llm_budget_entry (
    id TEXT PRIMARY KEY,
    month TEXT NOT NULL,                          -- YYYY-MM (UTC)
    kind TEXT NOT NULL CHECK (kind IN ('RESERVE','SETTLE')),
    reservation_id TEXT,                          -- SETTLE rows point at their RESERVE row
    amount_usd TEXT NOT NULL,                     -- RESERVE: worst-case allowance; SETTLE: amount charged
    basis TEXT NOT NULL,                          -- how the amount was computed
    llm_call_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (reservation_id, kind)
);
CREATE INDEX ix_llm_budget_month ON llm_budget_entry(month, kind);
CREATE TRIGGER llm_budget_entry_no_update BEFORE UPDATE ON llm_budget_entry
BEGIN SELECT RAISE(ABORT, 'llm_budget_entry is append-only'); END;
CREATE TRIGGER llm_budget_entry_no_delete BEFORE DELETE ON llm_budget_entry
BEGIN SELECT RAISE(ABORT, 'llm_budget_entry is append-only'); END;
