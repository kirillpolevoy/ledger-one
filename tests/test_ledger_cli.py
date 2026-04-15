import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from ledger_cli import add_override, list_overrides, remove_override  # noqa: E402


def _seed(db):
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Chase')")
    db.execute(
        "INSERT INTO transactions (id, account_id, amount, description, "
        "merchant_pattern, category, posted_at, categorization_source) "
        "VALUES ('t1', 'a1', -5, 'STARBUCKS #1234', 'starbucks', 'Restaurants', now(), 'ai')"
    )


def test_add_normalizes_and_retroactively_updates(db):
    _seed(db)
    count = add_override(db, "STARBUCKS #9999", "Coffee")
    assert count == 1
    row = db.execute("SELECT category FROM transactions WHERE id='t1'").fetchone()
    assert row == ("Coffee",)
    assert ("starbucks", "Coffee") in list_overrides(db)


def test_remove(db):
    add_override(db, "starbucks", "Coffee")
    remove_override(db, "starbucks")
    assert list_overrides(db) == []
