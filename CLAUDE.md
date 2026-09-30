# CLAUDE.md — operative rules for this repository

Personal long-only fundamental investing research + portfolio monitor. It never trades.

- Keep all work in this repo. Do not read, modify or reuse data from `realizeit7/Financial_exp`; its BTC holdout
  2025-09-01..2026-08-31 is sealed.
- Missing data is `None` with a reason, never zero. Recommendations never modify the ledger.
- Ledger, theses, valuations, recommendations and decisions are append-only (DB triggers). Corrections are new rows.
- Decisions use `public_at <= as_of` only. LLM output is untrusted: strict schema, verified citations, drafts only.
- Every threshold lives in `config/policy.yaml` / `config/models.py`; document changes in POLICY.md.
- Label outputs ACTUAL / PAPER / FIXTURE / ILLUSTRATIVE / HYPOTHETICAL and PREVIEW. No performance claims from fixtures.
- Tests must stay offline and credential-free: `uv run pytest`.
