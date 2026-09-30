# CSV import formats

## Transactions (`eqm import transactions <account> <file>`)

UTF-8 (BOM tolerated), header row required, one event per row (except `DIVIDEND_REINVEST`, which creates a linked
DIVIDEND + BUY pair). Required columns: `date`, `type`. Unknown columns are rejected (to catch typos).
Full example: `examples/transactions.example.csv`.

| column | meaning |
|---|---|
| `date` | trade/effective date `YYYY-MM-DD` (ex-date for splits) |
| `settle_date` | optional; default = trade date + account settlement days (T+1) on NYSE sessions |
| `time`, `timezone` | optional execution time `HH:MM` and IANA zone (default `America/New_York`); stored in UTC |
| `type` | `DEPOSIT`, `WITHDRAWAL`, `BUY`, `SELL`, `FEE`, `DIVIDEND`, `INTEREST`, `SPLIT`, `OPENING_POSITION`, `OPENING_CASH`, `DIVIDEND_REINVEST`, `CORPORATE_ACTION` |
| `symbol` | ticker; required for security events |
| `security_type` | optional `COMMON`, `ETF`, `ADR`, `FUND`, ... (known ETFs such as SCHG are recognized) |
| `quantity` | shares, positive (fractional allowed) |
| `price` | per-share price in USD |
| `fees` | commission/fees in USD, ≥ 0, charged to cash (BUY: added to basis; SELL: deducted from proceeds) |
| `amount` | cash amount for DEPOSIT/WITHDRAWAL/FEE/DIVIDEND/INTEREST/OPENING_CASH (positive) and DIVIDEND_REINVEST |
| `currency` | only `USD` is supported (other values are rejected) |
| `cost_basis` | OPENING_POSITION total basis; **leave blank if unknown** (it stays unknown, never zero) |
| `acquired_unknown` | `true` if an opening position's acquisition date is unknown (holding period → unknown) |
| `split_from`, `split_to` | SPLIT ratio: a 4-for-1 split is `split_from=1, split_to=4` |
| `subtype` | CORPORATE_ACTION kind (`MERGER`, `SPINOFF`, ...): not applied; opens a reconciliation issue |
| `link_id` | optional grouping id (e.g. dividend + reinvestment) |
| `external_id` | broker transaction id — **strongly recommended**; it is the dedupe key |
| `note` | free text |
| `account` | informational; the target account is the CLI argument |

Rules
- **Re-importing is safe**: each row's dedupe key is `external_id`, or else a hash of the normalized row plus its occurrence
  index among identical rows in the same file. Identical rows inside one file are distinct events; importing a file twice adds nothing.
- Rows that would create negative holdings (long-only), use unsupported currencies, or break existing history are **rejected
  and reported**, not coerced. Other rows in the file still import.
- History is append-only. Fix a mistake with `eqm ledger reverse --event-id ... --reason ...` and, if needed, add the
  corrected event; both stay in the audit trail. A reversal that would make later history invalid (e.g. reversing a
  buy that a later sale depends on) is rejected: reverse the later event first.
- Negative cash (usually a missing deposit/opening balance) opens a NEGATIVE_CASH reconciliation warning.

## Brokerage snapshot (`eqm import snapshot <account> <file> --as-of YYYY-MM-DD`)

Columns: `type` (`POSITION` or `CASH`), `symbol`, `quantity`, `cash`, `market_value`, `cost_basis`. The ledger is replayed
to the snapshot date and every difference (> 0.0001 shares, > $0.01 cash) becomes a CRITICAL reconciliation issue. The
ledger is never overwritten. Example: `examples/snapshot.example.csv`.

## Prices (`market_data_provider: csv`, files at `<home>/prices_csv/<SYMBOL>.csv`)

Columns: `date,open,high,low,close,volume[,dividend,split_ratio]`. Closes must be **raw (unadjusted)**; a split is
`split_ratio=4:1` on its ex-date and a dividend is the cash amount per share on its ex-date.
