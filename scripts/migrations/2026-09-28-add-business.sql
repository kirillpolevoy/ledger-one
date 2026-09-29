-- Add the `business` flag: category = what it is, business = who pays.
--
-- transactions.business marks a charge as a work expense (reimbursed or billed
-- elsewhere). category_overrides.business makes a merchant rule also tag its
-- new charges business on pull. Both default to false (personal).
--
-- Safe on Postgres 11+: non-volatile DEFAULT stores only metadata; no table rewrite,
-- so ACCESS EXCLUSIVE lock is held for microseconds even on populated tables.
-- The partial index starts empty (every row is false until tagged).
--
-- Apply this BEFORE deploying code that reads or writes `business` — the pull
-- INSERTs it and the companion UI/digest SELECT it, so they fail without it.
--
-- Converting a legacy 'Business Expense' category, in this order:
--   1. Apply this file.
--   2. Deploy the new pull code (merge to main — the scheduled pull runs from
--      main). An older pull files charges from a converted "always business"
--      override with business = false; re-running steps 4-6 re-tags them.
--   3. Remove 'Business Expense' from config/categories.yaml.
--   4. python scripts/migrate_business_expense.py
--      Dry run: prints the report, rolls back, writes business_migration_plan.json
--      (each row's and override's new category).
--   5. Review the plan; fix any category the AI got wrong. Keep it out of git.
--   6. python scripts/migrate_business_expense.py --apply --plan business_migration_plan.json
--      Commits exactly the plan, no AI calls. Aborts with nothing written if the
--      Business Expense rows/overrides changed since step 4.
--
-- Rollback (only after reverting that code):
--   DROP INDEX IF EXISTS idx_transactions_business;
--   ALTER TABLE transactions DROP COLUMN business;
--   ALTER TABLE category_overrides DROP COLUMN business;
-- Rolling back loses every business tag; the old 'Business Expense' category is
-- not restored.
--
-- Apply: psql "$DATABASE_URL" -f scripts/migrations/2026-09-28-add-business.sql
-- Run outside the 18:00 UTC cron window to avoid racing an in-flight pull.

ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS business BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE category_overrides
  ADD COLUMN IF NOT EXISTS business BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_transactions_business
  ON transactions (business)
  WHERE business;
