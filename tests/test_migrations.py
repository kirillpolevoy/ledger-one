from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "scripts" / "migrations"


def test_add_business_migration_upgrades_old_schema_idempotently(db):
    # Roll the fresh schema back to its pre-business shape, with data in it.
    db.execute("""
        DROP INDEX idx_transactions_business;
        ALTER TABLE transactions DROP COLUMN business;
        ALTER TABLE category_overrides DROP COLUMN business;
    """)
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Chase')")
    db.execute(
        "INSERT INTO transactions (id, account_id, amount, posted_at) "
        "VALUES ('t1', 'a1', -5, now())"
    )
    db.execute(
        "INSERT INTO category_overrides (merchant_pattern, category) "
        "VALUES ('uber trip', 'Transportation')"
    )

    sql = (MIGRATIONS / "2026-09-28-add-business.sql").read_text()
    db.execute(sql)
    db.execute(sql)  # idempotent: second apply is a no-op

    assert db.execute("SELECT business FROM transactions").fetchall() == [(False,)]
    assert db.execute("SELECT business FROM category_overrides").fetchall() == [(False,)]
    cols = db.execute("""
        SELECT table_name, is_nullable, column_default FROM information_schema.columns
        WHERE column_name = 'business' ORDER BY table_name
    """).fetchall()
    assert cols == [
        ("category_overrides", "NO", "false"),
        ("transactions", "NO", "false"),
    ]
    idx = db.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_transactions_business'"
    ).fetchone()
    assert idx is not None and "WHERE business" in idx[0]
