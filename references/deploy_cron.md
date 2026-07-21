# Deploying the daily pull and weekly digest

## Option 1: GitHub Actions (recommended)

The code can live in a public or private repo. Secrets must stay in GitHub Actions secrets, local env files, or your hosting provider's secret store. Then:

1. Add three repo secrets in Settings → Secrets and variables → Actions:
   - `SIMPLEFIN_ACCESS_URL`
   - `DATABASE_URL`
   - `ANTHROPIC_API_KEY`
2. The workflow at `.github/workflows/pull.yml` runs daily at 18:00 UTC. Adjust the cron expression as desired, but fire it *after* your bank's daily SimpleFIN refresh (see "Picking a cron time" below).
3. Trigger a manual run from the Actions tab to verify.

SimpleFIN enforces a limit of ~24 API calls per day per access token. Once daily is well under that.

## Option 2: Railway

1. Create a Railway project from this repo.
2. Add the three secrets as environment variables.
3. Enable the "Cron" feature and set the schedule to `0 18 * * *` running `python scripts/pull.py --days 32` (see "Pull window" below — do not narrow this).

## Option 3: Local crontab

```
0 18 * * * cd /path/to/ledger-one && .venv/bin/python scripts/pull.py --days 32 >> /tmp/ledger-one.log 2>&1
```

Make sure `.env` is present at the project root so `python-dotenv` picks up the credentials.

## Pull window: why `--days 32`

The pull reconciles pending rows by feed absence: a pending inside the refetch window that SimpleFIN no longer reports gets deleted (it settled under a new id, or the hold was released). That's only sound if every still-live pending is re-fetched on every run — 32 days covers ~31-day hotel/rental holds, and a 2-day buffer at the window edge protects against SimpleFIN's day-precision date filter. Pulling more days is near-free: only genuinely new transactions get categorized. If you narrow the window, pendings older than `days - 2` silently stop being reconciled. (`--days 32` is also the CLI default, so bare manual runs reconcile the same way as the cron.)

## Picking a cron time

SimpleFIN syncs each linked bank on its own ~24h cadence. If your cron fires *before* SimpleFIN's daily refresh for a given bank, that day's new transactions land in the pull the *next* day — one-day lag, indefinitely. Pick a UTC hour that falls after the latest-refreshing bank — watch the `balance-date` each account reports over a few days to learn its refresh time.

For US East Chase, SimpleFIN refresh has been observed around 13:00 UTC; 18:00 UTC gives a ~5h buffer for jitter. If you add a bank later that refreshes later in the day, shift the cron.

## Failure behavior

The pull script exits **non-zero** (GitHub Actions run goes red) when:
- Required env vars are missing (`SIMPLEFIN_ACCESS_URL`, `DATABASE_URL`, `ANTHROPIC_API_KEY`).
- The `SIMPLEFIN_ACCESS_URL` fails validation (not HTTPS, or host doesn't match `*.simplefin.org`).
- An account's `balance_date` is more than 36h behind `now()` — this is how silent SimpleFIN bank-connection staleness gets surfaced, since `errors: []` in the payload does **not** guarantee the bank connection is healthy. Re-authenticate the flagged bank at [bridge.simplefin.org](https://beta-bridge.simplefin.org).

Expected successful run output ends with a one-line `Pull complete: {...stats...}`. Full key set: `accounts`, `transactions_fetched`, `pending_inserts`, `posted_inserts`, `pending_to_posted_transitions`, `duplicate_pending_suspects`, `pendings_dropped`, `override_matches`, `learned_matches`, `ai_calls`, `upserted`, `dry_run`, `errors`, `stale_accounts`.

- `pendings_dropped` counts stale pendings deleted by feed-absence reconciliation; each dropped row (date, description, amount, id) is logged just above the stats line. Nonzero values are routine.
- If `duplicate_pending_suspects > 0`, Chase (or another bank) may be re-issuing transaction IDs on pending→posted — see the upsert design note in the README.
- Reconciliation is skipped (with a log line saying why) when the feed is empty or SimpleFIN reported errors — absence can't be trusted on incomplete data.

## Weekly digest trigger

`.github/workflows/digest.yml` (Mondays 19:00 UTC — one hour after the daily pull, so posted transactions settle first) runs no digest logic itself. It curls an HTTPS endpoint in your companion layer (see `references/extending.md`), which builds and sends the digest and records a `digest_runs` row on its side. This repo is only the cron trigger; the endpoint, email delivery, and the `digest_runs` table all live in the companion repo, not in this schema.

Setup:

1. Deploy a digest endpoint in your companion app.
2. Add two repo secrets: `LEDGER_DIGEST_URL` (the endpoint URL) and `LEDGER_DIGEST_CRON_SECRET` (sent as `Authorization: Bearer ...`; the endpoint must verify it).
3. Verify with a manual run from the Actions tab using the `dry_run` input — it appends `?dryRun=true`, so no email is sent and no `digest_runs` row is written. Scheduled runs always hit the live path.

Not running a digest? Delete `digest.yml` or disable the workflow in the Actions tab — with the secrets unset it fails loudly (red run) rather than silently skipping.

## Caveats

- **Never commit `.env` or `.env.test`.** They are local secret files and should stay out of git.
- **Secret masking:** GitHub Actions masks secrets that match exact strings. `ledger-one` never prints the access URL, but if you add logging, audit it first.
- **Per-account bank errors:** the SimpleFIN payload's own `errors`/`errlist` fields are logged as warnings but do not fail the run. Stale balance-date is the gate that actually fails runs.
