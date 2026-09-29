import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from ledger_cli import add_override, format_override, list_overrides, remove_override  # noqa: E402


def _seed(db):
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Chase')")
    db.execute(
        "INSERT INTO transactions (id, account_id, amount, description, "
        "merchant_pattern, category, posted_at, categorization_source) "
        "VALUES ('t1', 'a1', -5, 'STARBUCKS #1234', 'starbucks', 'Restaurants', now(), 'ai')"
    )


def _add_tx(db, tx_id, pattern, category, source, *, business=False):
    db.execute(
        "INSERT INTO transactions (id, account_id, amount, description, "
        "merchant_pattern, category, posted_at, categorization_source, business) "
        "VALUES (%s, 'a1', -20, %s, %s, %s, now(), %s, %s)",
        (tx_id, pattern.upper(), pattern, category, source, business),
    )


def test_add_normalizes_and_retroactively_updates(db):
    _seed(db)
    result = add_override(db, "STARBUCKS #9999", "Coffee")
    assert result.pattern == "starbucks"
    assert result.recategorized == 1
    assert result.manual_kept == 0
    assert result.business_tagged == 0
    row = db.execute("SELECT category FROM transactions WHERE id='t1'").fetchone()
    assert row == ("Coffee",)
    assert ("starbucks", "Coffee", False) in list_overrides(db)


def test_add_skips_manual_rows(db):
    """Manual wins: the retroactive UPDATE leaves rows the user set by hand."""
    _seed(db)
    _add_tx(db, "t2", "starbucks", "Restaurants", "manual")
    _add_tx(db, "t3", "starbucks", "Coffee", "manual")  # already matches — not "kept"
    # NULL source (direct SQL, old rows) is not manual — it must still be
    # recategorized. Guards the NULL-safe IS DISTINCT FROM against a "<>" rewrite.
    _add_tx(db, "t4", "starbucks", "Restaurants", None)
    result = add_override(db, "starbucks", "Coffee")
    assert (result.recategorized, result.manual_kept) == (2, 1)
    rows = {
        r[0]: (r[1], r[2]) for r in
        db.execute("SELECT id, category, categorization_source FROM transactions").fetchall()
    }
    assert rows == {
        "t1": ("Coffee", "override"),
        "t2": ("Restaurants", "manual"),
        "t3": ("Coffee", "manual"),
        "t4": ("Coffee", "override"),
    }


def test_add_business_tags_retroactively(db):
    """--business tags every past charge from the merchant, manual rows
    included — manual-wins protects category, not the business flag."""
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Chase')")
    _add_tx(db, "t1", "dayton express", "Transportation", "ai")
    _add_tx(db, "t2", "dayton express", "Restaurants", "manual")
    _add_tx(db, "t3", "dayton express", "Travel", "ai", business=True)
    _add_tx(db, "t4", "starbucks", "Coffee", "ai")
    result = add_override(db, "DAYTON EXPRESS", "Travel", business=True)
    assert result.business_tagged == 2  # t3 was already business
    # t1 recategorized; t2 manual kept; t3 already Travel so not counted.
    assert (result.recategorized, result.manual_kept) == (1, 1)
    rows = dict(db.execute("SELECT id, business FROM transactions").fetchall())
    assert rows == {"t1": True, "t2": True, "t3": True, "t4": False}
    assert ("dayton express", "Travel", True) in list_overrides(db)


def test_add_without_business_clears_override_flag_but_never_untags(db):
    """Re-adding without --business turns the rule off for future pulls; past
    tags stay (business retro only ever adds)."""
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Chase')")
    _add_tx(db, "t1", "dayton express", "Travel", "ai")
    add_override(db, "dayton express", "Travel", business=True)
    result = add_override(db, "dayton express", "Travel")
    assert result.business_tagged == 0
    assert list_overrides(db) == [("dayton express", "Travel", False)]
    assert db.execute("SELECT business FROM transactions WHERE id='t1'").fetchone() == (True,)


def test_add_business_does_not_touch_learned(db):
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Chase')")
    _add_tx(db, "t1", "dayton express", "Travel", "ai")
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('dayton express', 'Travel')")
    add_override(db, "dayton express", "Travel", business=True)
    assert db.execute("SELECT merchant_pattern, category FROM merchant_categories").fetchall() == [
        ("dayton express", "Travel"),
    ]


def test_format_override_marks_business():
    assert format_override("dayton express", "Travel", True) == "dayton express → Travel [business]"
    assert format_override("starbucks", "Coffee", False) == "starbucks → Coffee"


def test_remove(db):
    add_override(db, "starbucks", "Coffee")
    remove_override(db, "starbucks")
    assert list_overrides(db) == []
