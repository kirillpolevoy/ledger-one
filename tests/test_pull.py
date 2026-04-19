import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from ledger_one.pull import run_pull, _warn_on_stale_balances, _classify_txns


def test_pull_end_to_end_learned_cache_hit(db, tmp_path):
    cats_file = tmp_path / "categories.yaml"
    cats_file.write_text("categories:\n  - Coffee\n  - Groceries\n")
    db.execute(
        "INSERT INTO merchant_categories (merchant_pattern, category) "
        "VALUES ('starbucks', 'Coffee')"
    )

    fresh_now = datetime.now(timezone.utc).isoformat()
    fake_accounts = [{
        "id": "a1", "name": "Chase", "institution": "Chase",
        "currency": "USD", "balance": "1000",
        "balance_date": fresh_now,
    }]
    fake_txns = [{
        "id": "tx1", "account_id": "a1", "amount": "-5",
        "description": "STARBUCKS #1234 SEATTLE WA",
        "posted_at": "2026-04-14T12:00:00+00:00",
        "pending": False, "has_real_posted": True,
        "raw_payload": {},
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
    assert stats["posted_inserts"] == 1
    assert stats["pending_inserts"] == 0
    assert stats["pending_to_posted_transitions"] == 0
    assert stats["duplicate_pending_suspects"] == 0
    assert stats["learned_matches"] == 1
    assert stats["ai_calls"] == 0
    assert stats["upserted"] == 1
    row = db.execute(
        "SELECT category, merchant_pattern, categorization_source, pending "
        "FROM transactions WHERE id='tx1'"
    ).fetchone()
    assert row == ("Coffee", "starbucks", "learned", False)
    assert stats["stale_accounts"] == []


def test_pull_inserts_pending_then_transitions_to_posted_preserving_category(db, tmp_path):
    cats_file = tmp_path / "categories.yaml"
    cats_file.write_text("categories:\n  - Coffee\n")
    db.execute(
        "INSERT INTO merchant_categories (merchant_pattern, category) "
        "VALUES ('starbucks', 'Coffee')"
    )

    fake_accounts = [{
        "id": "a1", "name": "Chase", "institution": "Chase",
        "currency": "USD", "balance": "1000",
        "balance_date": "2026-04-18T18:00:00+00:00",
    }]
    pending_txn = {
        "id": "tx1", "account_id": "a1", "amount": "-5.00",
        "description": "STARBUCKS #1234",
        "posted_at": "2026-04-18T14:00:00+00:00",  # transacted_at
        "pending": True, "has_real_posted": False,
        "raw_payload": {"pending": True},
    }
    anthropic = MagicMock()
    stats1 = run_pull(
        db=db, access_url="https://fake", days=7,
        categories_file=cats_file, anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
        simplefin_fetcher=lambda u, d: (fake_accounts, [pending_txn], []),
    )
    assert stats1["pending_inserts"] == 1
    assert stats1["pending_to_posted_transitions"] == 0

    row = db.execute(
        "SELECT pending, amount, category, categorization_source "
        "FROM transactions WHERE id='tx1'"
    ).fetchone()
    assert row == (True, -5, "Coffee", "learned")

    # Now transition: same id, pending=false, has_real_posted=true, new amount
    posted_txn = {
        "id": "tx1", "account_id": "a1", "amount": "-5.25",
        "description": "STARBUCKS #1234",
        "posted_at": "2026-04-19T08:00:00+00:00",  # real posted timestamp
        "pending": False, "has_real_posted": True,
        "raw_payload": {},
    }
    stats2 = run_pull(
        db=db, access_url="https://fake", days=7,
        categories_file=cats_file, anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
        simplefin_fetcher=lambda u, d: (fake_accounts, [posted_txn], []),
    )
    assert stats2["pending_inserts"] == 0
    assert stats2["posted_inserts"] == 0
    assert stats2["pending_to_posted_transitions"] == 1
    assert stats2["ai_calls"] == 0  # transitions don't re-categorize

    row = db.execute(
        "SELECT pending, amount, category, categorization_source "
        "FROM transactions WHERE id='tx1'"
    ).fetchone()
    # pending flipped, amount updated, category + source preserved
    assert row[0] is False
    assert str(row[1]) == "-5.25"
    assert row[2] == "Coffee"
    assert row[3] == "learned"


def test_pull_transitions_on_flip_moment_even_if_payload_still_pending(db, tmp_path):
    """Payload arrives with pending=true AND a real posted timestamp (has_real_posted=True).
    The transition must still be detected and DB pending flipped to false."""
    cats_file = tmp_path / "categories.yaml"
    cats_file.write_text("categories:\n  - Shopping\n")
    db.execute(
        "INSERT INTO merchant_categories (merchant_pattern, category) "
        "VALUES ('amazon.com', 'Shopping')"
    )
    fake_accounts = [{
        "id": "a1", "name": "Chase", "institution": "Chase",
        "currency": "USD", "balance": "0",
        "balance_date": "2026-04-18T18:00:00+00:00",
    }]
    # Seed pending
    pending = {
        "id": "tx2", "account_id": "a1", "amount": "-10.00",
        "description": "AMAZON.COM", "posted_at": "2026-04-17T14:00:00+00:00",
        "pending": True, "has_real_posted": False, "raw_payload": {},
    }
    anthropic = MagicMock()
    run_pull(
        db=db, access_url="https://fake", days=7,
        categories_file=cats_file, anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
        simplefin_fetcher=lambda u, d: (fake_accounts, [pending], []),
    )

    # Flip moment: pending=true AND has_real_posted=true
    flip = {
        "id": "tx2", "account_id": "a1", "amount": "-10.00",
        "description": "AMAZON.COM", "posted_at": "2026-04-18T08:00:00+00:00",
        "pending": True, "has_real_posted": True, "raw_payload": {},
    }
    stats = run_pull(
        db=db, access_url="https://fake", days=7,
        categories_file=cats_file, anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
        simplefin_fetcher=lambda u, d: (fake_accounts, [flip], []),
    )
    assert stats["pending_to_posted_transitions"] == 1
    assert db.execute("SELECT pending FROM transactions WHERE id='tx2'").fetchone() == (False,)


def test_pull_flags_duplicate_pending_suspects_when_id_rotates(db, tmp_path):
    """If Chase re-issues the id on pending→posted, the new posted row looks like
    truly_new to us. The heuristic should flag it against the lingering pending."""
    cats_file = tmp_path / "categories.yaml"
    cats_file.write_text("categories:\n  - Shopping\n")
    db.execute(
        "INSERT INTO merchant_categories (merchant_pattern, category) "
        "VALUES ('amazon.com', 'Shopping')"
    )
    fake_accounts = [{
        "id": "a1", "name": "Chase", "institution": "Chase",
        "currency": "USD", "balance": "0",
        "balance_date": "2026-04-18T18:00:00+00:00",
    }]
    # Seed a pending row with id=tx-pending-abc
    pending = {
        "id": "tx-pending-abc", "account_id": "a1", "amount": "-42.00",
        "description": "AMAZON.COM", "posted_at": "2026-04-17T14:00:00+00:00",
        "pending": True, "has_real_posted": False, "raw_payload": {},
    }
    anthropic = MagicMock()
    run_pull(
        db=db, access_url="https://fake", days=7,
        categories_file=cats_file, anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
        simplefin_fetcher=lambda u, d: (fake_accounts, [pending], []),
    )

    # Now a posted row with a DIFFERENT id but same (account_id, amount, merchant_pattern)
    posted_rotated = {
        "id": "tx-posted-xyz",  # different id!
        "account_id": "a1", "amount": "-42.00",
        "description": "AMAZON.COM", "posted_at": "2026-04-18T08:00:00+00:00",
        "pending": False, "has_real_posted": True, "raw_payload": {},
    }
    stats = run_pull(
        db=db, access_url="https://fake", days=7,
        categories_file=cats_file, anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
        simplefin_fetcher=lambda u, d: (fake_accounts, [posted_rotated], []),
    )
    assert stats["posted_inserts"] == 1  # inserted as new, because id doesn't match
    assert stats["duplicate_pending_suspects"] == 1  # BUT flagged


def test_warn_on_stale_balances_flags_old_accounts(caplog):
    now = datetime(2026, 4, 18, 18, 0, tzinfo=timezone.utc)
    accounts = [
        {"name": "Chase CC", "institution": "Chase", "balance_date": "2026-04-16T13:00:00+00:00"},  # 53h stale
        {"name": "First Am", "institution": "First American Bank", "balance_date": "2026-04-18T06:00:00+00:00"},  # 12h fresh
        {"name": "Unknown", "institution": None, "balance_date": None},  # no date — skipped
    ]
    with caplog.at_level(logging.WARNING, logger="ledger_one.pull"):
        stale = _warn_on_stale_balances(accounts, now=now)
    assert stale == ["Chase CC"]
    assert any("Chase CC" in r.message and "53.0h" in r.message for r in caplog.records)


def test_classify_txns_buckets_correctly():
    existing = {
        "in-db-pending-still-pending": True,
        "in-db-pending-now-posted": True,
        "in-db-posted": False,
    }
    txns = [
        # Not in existing → truly_new
        {"id": "new-one", "pending": False, "has_real_posted": True},
        # Pending in DB, still pending in payload → already_seen
        {"id": "in-db-pending-still-pending", "pending": True, "has_real_posted": False},
        # Pending in DB, now has real posted timestamp → transition
        {"id": "in-db-pending-now-posted", "pending": False, "has_real_posted": True},
        # Already posted in DB → already_seen (WHERE guard would block anyway)
        {"id": "in-db-posted", "pending": False, "has_real_posted": True},
    ]
    truly_new, transitions, already_seen = _classify_txns(txns, existing)
    assert [t["id"] for t in truly_new] == ["new-one"]
    assert [t["id"] for t in transitions] == ["in-db-pending-now-posted"]
    assert [t["id"] for t in already_seen] == [
        "in-db-pending-still-pending", "in-db-posted",
    ]
