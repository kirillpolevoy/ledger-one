# ledger-one — agent guide

Data layer for personal finance: pulls SimpleFIN transactions (pending and posted) into the user's own Postgres and categorizes them (overrides → learned patterns → Claude). No UI. End-user setup walkthrough: [SKILL.md](SKILL.md). Task-specific docs: [references/](references/).

## Hard rules

- **Amount sign:** negative = debit (money out), positive = credit (money in).
- **Settled spend:** add `AND NOT pending` to any historical or analytical query. Query patterns: [references/querying_data.md](references/querying_data.md).
- **Writes:** external code writes only to `category_overrides`, plus `UPDATE transactions SET category = ...` for manual recategorization (a DB trigger maintains `merchant_categories`). Never write to other tables. Full contract: [references/extending.md](references/extending.md).
- **Rows can disappear:** the pull hard-DELETEs pending rows the bank stops reporting (feed-absence reconciliation). A vanished pending is expected behavior, not data loss — the settled charge, if any, is already there under its own id. Don't cache or foreign-key `transactions.id` for pending rows.
- **Secrets:** never print, paste, or log `SIMPLEFIN_ACCESS_URL`, `DATABASE_URL`, or API keys. They live only in `.env`, `.env.test`, or Actions secrets. See [SECURITY.md](SECURITY.md).

## Commands

- Install: `pip install -e ".[dev]"`
- Tests: `pytest`
- Pull: `python scripts/pull.py` (defaults to `--days 32`; add `--dry-run` to preview writes and would-drop pendings). Don't narrow the window — pending reconciliation depends on it; see [references/deploy_cron.md](references/deploy_cron.md).
- Add an override: `python scripts/ledger_cli.py override add "STARBUCKS" "Coffee"`

## Monitoring

A successful pull ends with one line — `Pull complete: {...stats...}` — and exits non-zero on failure (missing env vars, invalid SimpleFIN URL, or a bank connection whose `balance_date` is >36h stale). The full stats key set, including `pendings_dropped`, is documented in [references/deploy_cron.md](references/deploy_cron.md).

## Automation

Two GitHub Actions workflows: the daily pull ([.github/workflows/pull.yml](.github/workflows/pull.yml), 18:00 UTC) and a weekly digest trigger ([.github/workflows/digest.yml](.github/workflows/digest.yml), Mondays 19:00 UTC) that curls a companion-layer endpoint. The digest logic, email delivery, and `digest_runs` table live in the companion repo — this repo only fires the cron.
