import pytest
from ledger_one.config import load_categories, UNCATEGORIZED


def test_loads_and_injects_uncategorized(tmp_path):
    f = tmp_path / "cats.yaml"
    f.write_text("categories:\n  - Groceries\n  - Coffee\n")
    cats = load_categories(f)
    assert "Groceries" in cats and "Coffee" in cats
    assert UNCATEGORIZED in cats


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_categories(tmp_path / "nope.yaml")


def test_empty_raises(tmp_path):
    f = tmp_path / "cats.yaml"
    f.write_text("categories: []\n")
    with pytest.raises(ValueError):
        load_categories(f)
