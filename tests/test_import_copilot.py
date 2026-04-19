from datetime import date
from pathlib import Path
from ledger_one.import_copilot import import_csv

FIX = Path(__file__).parent / "fixtures" / "copilot_sample.csv"
CUTOFF = date(2026, 4, 15)


def _seed_account(db):
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Chase')")


def test_seeds_merchant_categories(db):
    _seed_account(db)
    stats = import_csv(db, FIX, account_id="a1", before=CUTOFF)
    # STARBUCKS appears 3 times across rows before cutoff: 2x Coffee, 1x Restaurants → Coffee wins
    row = db.execute(
        "SELECT category FROM merchant_categories WHERE merchant_pattern='starbucks'"
    ).fetchone()
    assert row == ("Coffee",)
    # At least starbucks and whole foods mkt
    assert stats["merchant_mappings"] >= 2


def test_respects_before_cutoff_for_transactions(db):
    _seed_account(db)
    stats = import_csv(db, FIX, account_id="a1", before=CUTOFF)
    count = db.execute(
        "SELECT count(*) FROM transactions WHERE categorization_source='copilot_import'"
    ).fetchone()[0]
    # 4 rows before 2026-04-15, 1 row on/after → only 4 imported
    assert count == 4
    assert stats["transactions_imported"] == 4


def test_imported_rows_are_not_pending(db):
    _seed_account(db)
    import_csv(db, FIX, account_id="a1", before=CUTOFF)
    # Copilot CSV is historical posted data. The import explicitly passes
    # False for the `pending` column — not reliant on the schema DEFAULT.
    # Assert that every copilot-imported row is posted (pending=false).
    rows = db.execute(
        "SELECT pending FROM transactions WHERE categorization_source='copilot_import'"
    ).fetchall()
    assert len(rows) > 0  # ensure we actually imported something
    assert all(r[0] is False for r in rows)


def test_idempotent(db):
    _seed_account(db)
    import_csv(db, FIX, account_id="a1", before=CUTOFF)
    s2 = import_csv(db, FIX, account_id="a1", before=CUTOFF)
    # Re-run inserts zero new transactions (deterministic IDs → ON CONFLICT DO NOTHING).
    assert s2["transactions_imported"] == 0
