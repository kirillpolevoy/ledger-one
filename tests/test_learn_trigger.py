def _seed(db):
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Test')")

def _tx(db, tx_id, pattern, category):
    db.execute(
        "INSERT INTO transactions (id, account_id, amount, description, "
        "merchant_pattern, category, posted_at) "
        "VALUES (%s, 'a1', -10, %s, %s, %s, now())",
        (tx_id, pattern, pattern, category),
    )

def test_trigger_creates_mapping(db):
    _seed(db)
    _tx(db, "t1", "starbucks", "Restaurants")
    db.execute("UPDATE transactions SET category = 'Coffee' WHERE id = 't1'")
    row = db.execute(
        "SELECT category FROM merchant_categories WHERE merchant_pattern='starbucks'"
    ).fetchone()
    assert row == ("Coffee",)

def test_trigger_latest_wins_on_conflict(db):
    _seed(db)
    db.execute(
        "INSERT INTO merchant_categories (merchant_pattern, category) VALUES ('amazon', 'Shopping')"
    )
    _tx(db, "t1", "amazon", "Shopping")
    db.execute("UPDATE transactions SET category = 'Groceries' WHERE id = 't1'")
    row = db.execute(
        "SELECT category FROM merchant_categories WHERE merchant_pattern='amazon'"
    ).fetchone()
    assert row == ("Groceries",)

def test_trigger_bulk_update_single_upsert_per_pattern(db):
    _seed(db)
    for i in range(10):
        _tx(db, f"t{i}", "starbucks", "Restaurants")
    db.execute("UPDATE transactions SET category = 'Coffee' WHERE merchant_pattern='starbucks'")
    row = db.execute(
        "SELECT category FROM merchant_categories WHERE merchant_pattern='starbucks'"
    ).fetchone()
    assert row == ("Coffee",)
