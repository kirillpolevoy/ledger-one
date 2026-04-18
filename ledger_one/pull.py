import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ledger_one.normalize import normalize_merchant
from ledger_one.categorize import categorize_transactions
from ledger_one.config import load_categories
from ledger_one.db import upsert_accounts, insert_transactions
from ledger_one.simplefin import fetch_accounts_and_transactions

log = logging.getLogger(__name__)

STALE_BALANCE_THRESHOLD = timedelta(hours=36)


def _warn_on_stale_balances(accounts, now=None):
    now = now or datetime.now(timezone.utc)
    stale = []
    for acct in accounts:
        bd = acct.get("balance_date")
        if not bd:
            continue
        age = now - datetime.fromisoformat(bd)
        if age > STALE_BALANCE_THRESHOLD:
            stale.append(acct["name"])
            log.warning(
                "Stale balance for %s (%s): %.1fh old — SimpleFIN may be serving cached data",
                acct["name"], acct.get("institution") or "?", age.total_seconds() / 3600,
            )
    return stale


def run_pull(
    *,
    db,
    access_url: str,
    days: int,
    categories_file: Path,
    anthropic_client,
    model: str,
    simplefin_fetcher=fetch_accounts_and_transactions,
    dry_run: bool = False,
) -> dict:
    accounts, raw_txns, errors = simplefin_fetcher(access_url, days)
    for err in errors:
        log.warning("SimpleFIN error: %s", err)
    stale_accounts = _warn_on_stale_balances(accounts)

    for tx in raw_txns:
        tx["merchant_pattern"] = normalize_merchant(tx["description"])

    existing: set[str] = set()
    if raw_txns:
        rows = db.execute(
            "SELECT id FROM transactions WHERE id = ANY(%s)",
            ([tx["id"] for tx in raw_txns],),
        ).fetchall()
        existing = {r[0] for r in rows}
    new_txns = [tx for tx in raw_txns if tx["id"] not in existing]

    categories = load_categories(categories_file)
    results = categorize_transactions(
        db, new_txns,
        categories=categories,
        anthropic_client=anthropic_client,
        model=model,
    )

    for tx in new_txns:
        cat, source = results[tx["id"]]
        tx["category"] = cat
        tx["source"] = source

    if dry_run:
        log.info("Dry run — skipping database writes.")
        for tx in new_txns:
            log.info("  %s | %s | %s | %s", tx["posted_at"][:10], tx["description"][:40], tx["amount"], tx["category"])
        inserted = 0
    else:
        with db.transaction():
            upsert_accounts(db, accounts)
            inserted = insert_transactions(db, new_txns)

    stats = {
        "accounts": len(accounts),
        "transactions_fetched": len(raw_txns),
        "new_transactions": len(new_txns),
        "override_matches": sum(1 for _, s in results.values() if s == "override"),
        "learned_matches": sum(1 for _, s in results.values() if s == "learned"),
        "ai_calls": sum(1 for _, s in results.values() if s == "ai"),
        "inserted": inserted,
        "dry_run": dry_run,
        "errors": errors,
        "stale_accounts": stale_accounts,
    }
    log.info("pull stats: %s", stats)
    return stats
