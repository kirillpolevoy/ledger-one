from unittest.mock import MagicMock
import anthropic as anthropic_pkg
from ledger_one.categorize import categorize_transactions


def _mock_tool_response(anthropic, classifications: dict):
    block = MagicMock()
    block.type = "tool_use"
    block.input = {"classifications": classifications}
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "tool_use"
    anthropic.messages.create.return_value = resp


def test_override_wins(db):
    db.execute("INSERT INTO category_overrides (merchant_pattern, category) "
               "VALUES ('starbucks', 'Coffee')")
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('starbucks', 'Restaurants')")
    anthropic = MagicMock()
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "starbucks", "description": "STARBUCKS #1"}],
        categories=["Coffee", "Restaurants"],
        anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
    )
    assert results["t1"] == ("Coffee", "override")
    anthropic.messages.create.assert_not_called()


def test_learned_wins_when_no_override(db):
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('whole foods mkt', 'Groceries')")
    anthropic = MagicMock()
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "whole foods mkt", "description": "WHOLE FOODS"}],
        categories=["Groceries", "Restaurants"],
        anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
    )
    assert results["t1"] == ("Groceries", "learned")
    anthropic.messages.create.assert_not_called()


def test_ai_fallback_for_unknown(db):
    anthropic = MagicMock()
    _mock_tool_response(anthropic, {"t1": "Coffee"})
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "some novel merchant", "description": "SOME NOVEL"}],
        categories=["Coffee", "Restaurants"],
        anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
    )
    assert results["t1"] == ("Coffee", "ai")
    anthropic.messages.create.assert_called_once()


def test_ai_api_error_falls_back_to_uncategorized(db):
    anthropic = MagicMock()
    anthropic.messages.create.side_effect = anthropic_pkg.APIStatusError(
        message="boom", response=MagicMock(), body=None
    )
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "novel", "description": "X"}],
        categories=["Coffee"],
        anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
    )
    assert results["t1"] == ("Uncategorized", "ai")


def test_ai_invalid_category_becomes_uncategorized(db):
    anthropic = MagicMock()
    _mock_tool_response(anthropic, {"t1": "MadeUpCategory"})
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "novel", "description": "X"}],
        categories=["Coffee"],
        anthropic_client=anthropic,
        model="claude-haiku-4-5-20251001",
    )
    assert results["t1"] == ("Uncategorized", "ai")
