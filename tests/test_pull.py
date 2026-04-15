from pathlib import Path
from unittest.mock import MagicMock
from ledger_one.pull import run_pull


def test_pull_end_to_end_learned_cache_hit(db, tmp_path):
    cats_file = tmp_path / "categories.yaml"
    cats_file.write_text("categories:\n  - Coffee\n  - Groceries\n")
    db.execute(
        "INSERT INTO merchant_categories (merchant_pattern, category) "
        "VALUES ('starbucks', 'Coffee')"
    )

    fake_accounts = [{
        "id": "a1", "name": "Chase", "institution": "Chase",
        "currency": "USD", "balance": "1000",
        "balance_date": "2026-04-15T00:00:00+00:00",
    }]
    fake_txns = [{
        "id": "tx1", "account_id": "a1", "amount": "-5",
        "description": "STARBUCKS #1234 SEATTLE WA",
        "posted_at": "2026-04-14T12:00:00+00:00", "raw_payload": {},
    }]

    anthropic = MagicMock()
    stats = run_pull(
        db=db,
        access_url="https://fake",
        days=7,
        categories_file=cats_file,
        anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
        simplefin_fetcher=lambda url, days: (fake_accounts, fake_txns, []),
    )
    assert stats["new_transactions"] == 1
    assert stats["learned_matches"] == 1
    assert stats["ai_calls"] == 0
    row = db.execute(
        "SELECT category, merchant_pattern, categorization_source "
        "FROM transactions WHERE id='tx1'"
    ).fetchone()
    assert row == ("Coffee", "starbucks", "learned")
