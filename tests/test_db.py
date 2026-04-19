from ledger_one.db import upsert_accounts, upsert_transactions


def _account(**overrides):
    base = {"id": "a1", "name": "Chase", "institution": "Chase Bank",
            "currency": "USD", "balance": "1000.00",
            "balance_date": "2026-04-15T00:00:00+00:00"}
    base.update(overrides)
    return base


def _txn(**overrides):
    base = {
        "id": "tx1", "account_id": "a1", "amount": "-5.00",
        "description": "STARBUCKS", "merchant_pattern": "starbucks",
        "category": "Coffee", "source": "ai",
        "posted_at": "2026-04-14T12:00:00+00:00", "raw_payload": {},
        "pending": False,
    }
    base.update(overrides)
    return base


def test_upsert_account_insert_then_update(db):
    upsert_accounts(db, [_account(balance="1000.00")])
    assert db.execute("SELECT last_balance FROM accounts WHERE id='a1'").fetchone() == (1000,)
    upsert_accounts(db, [_account(balance="1500.00")])
    assert db.execute("SELECT last_balance FROM accounts WHERE id='a1'").fetchone() == (1500,)


def test_upsert_transactions_inserts_posted(db):
    upsert_accounts(db, [_account()])
    inserted, updated = upsert_transactions(db, [_txn()])
    assert (inserted, updated) == (1, 0)
    row = db.execute("SELECT pending, category FROM transactions WHERE id='tx1'").fetchone()
    assert row == (False, "Coffee")


def test_upsert_transactions_inserts_pending(db):
    upsert_accounts(db, [_account()])
    inserted, updated = upsert_transactions(db, [_txn(pending=True)])
    assert (inserted, updated) == (1, 0)
    row = db.execute("SELECT pending, category FROM transactions WHERE id='tx1'").fetchone()
    assert row == (True, "Coffee")


def test_upsert_transactions_batch(db):
    upsert_accounts(db, [_account()])
    txns = [_txn(id=f"tx{i}", description=f"M{i}", merchant_pattern=f"m{i}") for i in range(100)]
    inserted, updated = upsert_transactions(db, txns)
    assert (inserted, updated) == (100, 0)


def test_upsert_transitions_pending_to_posted_preserves_category_fields(db):
    """Category, categorization_source, categorized_at MUST be preserved through
    pending→posted. Amount, posted_at, merchant_pattern, pending get updated.
    """
    upsert_accounts(db, [_account()])
    # Initial pending insert with category='Coffee', source='ai'
    upsert_transactions(db, [_txn(pending=True, amount="-5.00")])
    before = db.execute(
        "SELECT category, categorization_source, categorized_at "
        "FROM transactions WHERE id='tx1'"
    ).fetchone()

    # Transition: same id, pending=false, new amount+posted_at+merchant_pattern
    # category/source omitted by run_pull for transitions (our upsert still
    # accepts them but the UPDATE SET clause doesn't touch those columns).
    inserted, updated = upsert_transactions(db, [_txn(
        pending=False, amount="-5.25", merchant_pattern="starbucks coffee",
        posted_at="2026-04-15T08:00:00+00:00",
        category=None, source=None,  # transitions pass None; UPDATE SET ignores
    )])
    assert (inserted, updated) == (0, 1)

    after = db.execute(
        "SELECT pending, amount, posted_at, merchant_pattern, category, "
        "categorization_source, categorized_at "
        "FROM transactions WHERE id='tx1'"
    ).fetchone()
    # Updated fields
    assert after[0] is False
    assert str(after[1]) == "-5.25"
    assert after[3] == "starbucks coffee"
    # Preserved fields
    assert after[4] == before[0] == "Coffee"
    assert after[5] == before[1] == "ai"
    assert after[6] == before[2]


def test_upsert_noop_on_already_posted(db):
    """WHERE guard: if a row is already posted, a subsequent upsert must not
    mutate it even if payload has different amount or flipped pending."""
    upsert_accounts(db, [_account()])
    upsert_transactions(db, [_txn(amount="-5.00", pending=False)])
    # Try to overwrite with different amount and pending=true
    inserted, updated = upsert_transactions(db, [_txn(amount="-99.99", pending=True)])
    assert (inserted, updated) == (0, 0)  # guard blocked
    row = db.execute("SELECT pending, amount FROM transactions WHERE id='tx1'").fetchone()
    assert row == (False, -5)


def test_upsert_idempotent_on_pending(db):
    """Re-upserting the same pending txn is a no-op (same pending state, same values)."""
    upsert_accounts(db, [_account()])
    upsert_transactions(db, [_txn(pending=True)])
    inserted, updated = upsert_transactions(db, [_txn(pending=True)])
    # Second call: UPDATE fires (WHERE allows it since row is pending),
    # but values are identical. Still counts as 1 updated.
    assert (inserted, updated) == (0, 1)


def test_learn_trigger_noop_on_transition_when_category_unchanged(db):
    """Pending→posted with unchanged category must NOT re-seed merchant_categories
    (trigger's IS DISTINCT FROM guard should filter it out).
    """
    upsert_accounts(db, [_account()])
    upsert_transactions(db, [_txn(pending=True, category="Coffee")])

    # Capture the learn-trigger state AFTER the initial pending insert.
    # (Note: INSERT fires AFTER INSERT triggers if any exist; the project's
    # trg_ledger_one_learn is AFTER UPDATE only, so merchant_categories is
    # empty at this point.)
    before = db.execute(
        "SELECT count(*) FROM merchant_categories WHERE merchant_pattern='starbucks'"
    ).fetchone()[0]

    # Transition with same category='Coffee' — trigger fires but IS DISTINCT
    # FROM filter suppresses the INSERT into merchant_categories.
    upsert_transactions(db, [_txn(
        pending=False, posted_at="2026-04-15T08:00:00+00:00",
        category=None, source=None,  # preserved
    )])

    after = db.execute(
        "SELECT count(*) FROM merchant_categories WHERE merchant_pattern='starbucks'"
    ).fetchone()[0]
    # No new row seeded (trigger guard)
    assert after == before
