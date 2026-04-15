import logging
from pathlib import Path
from ledger_one.normalize import normalize_merchant
from ledger_one.categorize import categorize_transactions
from ledger_one.config import load_categories
from ledger_one.db import upsert_accounts, insert_transactions
from ledger_one.simplefin import fetch_accounts_and_transactions

log = logging.getLogger(__name__)


def run_pull(
    *,
    db,
    access_url: str,
    days: int,
    categories_file: Path,
    anthropic_client,
    model: str,
    simplefin_fetcher=fetch_accounts_and_transactions,
) -> dict:
    accounts, raw_txns, errors = simplefin_fetcher(access_url, days)
    for err in errors:
        log.warning("SimpleFIN error: %s", err)

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
        "errors": errors,
    }
    log.info("pull stats: %s", stats)
    return stats
