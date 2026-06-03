import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from ledger_one.pull import run_pull, _warn_on_stale_balances, _classify_txns


def _seed_amazon(db, tmp_path):
    """Categories file + a learned amazon.com→Shopping mapping so pulls in the
    reconciliation tests never hit the AI categorizer."""
    cats_file = tmp_path / "categories.yaml"
    cats_file.write_text("categories:\n  - Shopping\n")
    db.execute(
        "INSERT INTO merchant_categories (merchant_pattern, category) "
        "VALUES ('amazon.com', 'Shopping')"
    )
    return cats_file


def _fresh_account(now):
    return [{
        "id": "a1", "name": "Chase", "institution": "Chase",
        "currency": "USD", "balance": "0", "balance_date": now.isoformat(),
    }]


def _txn(tx_id, *, posted_at, pending, has_real_posted, amount="-10.00", desc="AMAZON.COM"):
    return {
        "id": tx_id, "account_id": "a1", "amount": amount, "description": desc,
        "posted_at": posted_at, "pending": pending,
        "has_real_posted": has_real_posted, "raw_payload": {},
    }


def _run(db, cats_file, fetch, *, days=7, dry_run=False):
    return run_pull(
        db=db, access_url="https://fake", days=days, categories_file=cats_file,
        anthropic_client=MagicMock(), model="m",
        simplefin_fetcher=fetch, dry_run=dry_run,
    )


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


def test_pull_drops_pending_absent_from_feed_within_window(db, tmp_path):
    """A pending that SimpleFIN stops reporting (within the refetch window) is
    dropped — it settled under a new id or the hold was released."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    pending = _txn("p1", posted_at=(now - timedelta(days=1)).isoformat(),
                   pending=True, has_real_posted=False, amount="-22.34")
    _run(db, cats_file, lambda u, d: (acct, [pending], []))
    assert db.execute("SELECT pending FROM transactions WHERE id='p1'").fetchone() == (True,)

    # Next pull: p1 gone from the feed; an unrelated posted row keeps the feed non-empty.
    filler = _txn("f1", posted_at=now.isoformat(), pending=False, has_real_posted=True)
    stats = _run(db, cats_file, lambda u, d: (acct, [filler], []))
    assert db.execute("SELECT 1 FROM transactions WHERE id='p1'").fetchone() is None
    assert stats["pendings_dropped"] == 1


def test_pull_keeps_pending_still_in_feed(db, tmp_path):
    """A pending SimpleFIN still reports is left untouched."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    pending = _txn("p1", posted_at=(now - timedelta(days=1)).isoformat(),
                   pending=True, has_real_posted=False)
    _run(db, cats_file, lambda u, d: (acct, [pending], []))
    stats = _run(db, cats_file, lambda u, d: (acct, [pending], []))
    assert stats["pendings_dropped"] == 0
    assert db.execute("SELECT pending FROM transactions WHERE id='p1'").fetchone() == (True,)


def test_pull_does_not_drop_pending_outside_window(db, tmp_path):
    """A pending older than the refetch window is never dropped — SimpleFIN
    wasn't queried for it, so its absence is uninformative."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    old_pending = _txn("p_old", posted_at=(now - timedelta(days=40)).isoformat(),
                       pending=True, has_real_posted=False)
    _run(db, cats_file, lambda u, d: (acct, [old_pending], []))

    filler = _txn("f1", posted_at=now.isoformat(), pending=False, has_real_posted=True)
    stats = _run(db, cats_file, lambda u, d: (acct, [filler], []))
    assert stats["pendings_dropped"] == 0
    assert db.execute("SELECT 1 FROM transactions WHERE id='p_old'").fetchone() == (1,)


def test_pull_does_not_drop_posted_rows_absent_from_feed(db, tmp_path):
    """Reconciliation only touches pending rows. A posted row absent from the
    feed (older than the fetch window) stays put."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    posted = _txn("s1", posted_at=(now - timedelta(days=1)).isoformat(),
                  pending=False, has_real_posted=True)
    _run(db, cats_file, lambda u, d: (acct, [posted], []))

    filler = _txn("f1", posted_at=now.isoformat(), pending=False, has_real_posted=True)
    stats = _run(db, cats_file, lambda u, d: (acct, [filler], []))
    assert stats["pendings_dropped"] == 0
    assert db.execute("SELECT 1 FROM transactions WHERE id='s1'").fetchone() == (1,)


def test_pull_dry_run_reports_would_drop_but_does_not_delete(db, tmp_path):
    """Dry run reports the would-drop count but leaves the row in place."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    pending = _txn("p1", posted_at=(now - timedelta(days=1)).isoformat(),
                   pending=True, has_real_posted=False)
    _run(db, cats_file, lambda u, d: (acct, [pending], []))

    filler = _txn("f1", posted_at=now.isoformat(), pending=False, has_real_posted=True)
    stats = _run(db, cats_file, lambda u, d: (acct, [filler], []), dry_run=True)
    assert stats["pendings_dropped"] == 1
    assert db.execute("SELECT pending FROM transactions WHERE id='p1'").fetchone() == (True,)


def test_pull_auto_cleans_orphan_pending_on_id_rotation(db, tmp_path):
    """The orphan case: a pending lingers, then the bank settles it under a NEW
    id with a different amount. The new posted row inserts; the stranded pending
    is dropped because it's gone from the feed — no amount matching needed."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    pending = _txn("p_amz", posted_at=(now - timedelta(days=1)).isoformat(),
                   pending=True, has_real_posted=False, amount="-22.34")
    _run(db, cats_file, lambda u, d: (acct, [pending], []))

    rotated = _txn("s_amz", posted_at=now.isoformat(), pending=False,
                   has_real_posted=True, amount="-16.37")
    stats = _run(db, cats_file, lambda u, d: (acct, [rotated], []))
    assert stats["posted_inserts"] == 1
    assert stats["pendings_dropped"] == 1
    assert db.execute("SELECT 1 FROM transactions WHERE id='p_amz'").fetchone() is None
    assert db.execute("SELECT pending FROM transactions WHERE id='s_amz'").fetchone() == (False,)


def test_pull_skips_reconciliation_when_feed_has_errors(db, tmp_path):
    """A partial feed (SimpleFIN errors) must not delete pendings — a missing
    account's pendings would look falsely absent."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    pending = _txn("p1", posted_at=(now - timedelta(days=1)).isoformat(),
                   pending=True, has_real_posted=False)
    _run(db, cats_file, lambda u, d: (acct, [pending], []))

    filler = _txn("f1", posted_at=now.isoformat(), pending=False, has_real_posted=True)
    stats = _run(db, cats_file, lambda u, d: (acct, [filler], ["account x failed"]))
    assert stats["pendings_dropped"] == 0
    assert db.execute("SELECT pending FROM transactions WHERE id='p1'").fetchone() == (True,)


def test_pull_skips_reconciliation_on_empty_feed(db, tmp_path):
    """An empty feed (likely an outage) must not nuke every pending."""
    cats_file = _seed_amazon(db, tmp_path)
    now = datetime.now(timezone.utc)
    acct = _fresh_account(now)
    pending = _txn("p1", posted_at=(now - timedelta(days=1)).isoformat(),
                   pending=True, has_real_posted=False)
    _run(db, cats_file, lambda u, d: (acct, [pending], []))

    stats = _run(db, cats_file, lambda u, d: (acct, [], []))
    assert stats["pendings_dropped"] == 0
    assert db.execute("SELECT pending FROM transactions WHERE id='p1'").fetchone() == (True,)


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
