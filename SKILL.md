---
name: ledger-one
description: Personal finance tracker that replaces Mint or Copilot Money. Pulls bank transactions from SimpleFIN into the user's own Postgres database, categorizes them using the user's history plus Claude. Use when the user says "track my spending", "pull my bank transactions", "categorize transactions", "import from Copilot", "set up a budgeting tool", "replace Mint", "replace Copilot Money", or asks anything about SimpleFIN, Neon, or personal finance data.
---

# ledger-one

Data layer for personal finance: pulls SimpleFIN transactions, categorizes them (overrides → learned patterns → Claude), and persists them in the user's own Postgres. No UI — query the data directly or build your own.

## When to use this skill
- User wants to track spending without Mint/Copilot
- User wants to pull transactions from their bank into their own database
- User has a Copilot Money CSV export and wants to keep their labeled history
- User wants Claude to categorize transactions

## First-time setup (Claude walks through these steps)

1. **SimpleFIN Bridge account + bank link.** See `references/simplefin_setup.md`.
2. **Claim token.** `python scripts/claim_token.py <base64-token>` → access URL.
3. **Neon database.** See `references/neon_setup.md`.
4. **Apply schema.** `psql "$DATABASE_URL" -f scripts/schema.sql` (or paste into Neon SQL editor).
5. **(Required if coming from Copilot) Import historical data.**
   - Export CSV from Copilot (`references/copilot_export.md`).
   - Pick a cutover date — the first day ledger-one will own forward. Typically today.
   - `python scripts/import_copilot.py ~/copilot.csv --account-id <id> --before YYYY-MM-DD`
6. **Configure categories.** Copy `config/categories.yaml.example` → `config/categories.yaml`, edit.
7. **Env vars.** Copy `.env.example` → `.env`.
8. **First pull.** `python scripts/pull.py --days 90`
9. **Cron.** See `references/deploy_cron.md` — GitHub Actions workflow included.

## Ongoing
- **Add an override:** `python scripts/ledger_cli.py override add "STARBUCKS" "Coffee"`
- **Recategorize a transaction:** just `UPDATE transactions SET category = '...'`. The DB trigger updates the learned mapping automatically.
- **Query data:** `references/querying_data.md`.

## Extending
See `references/extending.md` — private layer imports `ledger_one`, reads from the same DB, only writes to `category_overrides`.
