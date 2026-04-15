# Neon Postgres setup

1. Go to [https://console.neon.tech](https://console.neon.tech). Free tier is plenty.
2. Create a new project. Any region; pick the one closest to you. Postgres 17 recommended.
3. Copy the **connection string** from the project dashboard (it looks like `postgresql://user:pass@host/db?sslmode=require`).
4. Paste it into `.env` as `DATABASE_URL`.
5. Apply the schema:
   - Option A (terminal): `psql "$DATABASE_URL" -f scripts/schema.sql`
   - Option B (browser): open Neon SQL Editor, paste the contents of `scripts/schema.sql`, click Run.
6. Verify: `psql "$DATABASE_URL" -c "\dt"` should list `accounts`, `transactions`, `merchant_categories`, `category_overrides`.

**Backup branches:** Neon supports instant DB branching. Create a `test` branch in the console for throwaway test data (`TEST_DATABASE_URL` in `.env`).
