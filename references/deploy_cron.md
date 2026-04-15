# Deploying the daily pull

## Option 1: GitHub Actions (recommended)

Push this repo to a **private** GitHub repo (the SimpleFIN access URL contains credentials; never make the repo public with secrets). Then:

1. Add three repo secrets in Settings → Secrets and variables → Actions:
   - `SIMPLEFIN_ACCESS_URL`
   - `DATABASE_URL`
   - `ANTHROPIC_API_KEY`
2. The workflow at `.github/workflows/pull.yml` runs daily at 12:00 UTC. Adjust the cron expression as desired.
3. Trigger a manual run from the Actions tab to verify.

SimpleFIN enforces a limit of ~24 API calls per day per access token. Once daily is well under that.

## Option 2: Railway

1. Create a Railway project from this repo.
2. Add the three secrets as environment variables.
3. Enable the "Cron" feature and set the schedule to `0 12 * * *` running `python scripts/pull.py --days 7`.

## Option 3: Local crontab

```
0 12 * * * cd /path/to/ledger-one && .venv/bin/python scripts/pull.py --days 7 >> /tmp/ledger-one.log 2>&1
```

Make sure `.env` is present at the project root so `python-dotenv` picks up the credentials.

## Caveats

- **Secret masking:** GitHub Actions masks secrets that match exact strings. `ledger-one` never prints the access URL, but if you add logging, audit it first.
- **Flaky days:** banks drop SimpleFIN syncs occasionally. The pull script logs these and continues — no action needed.
