import json
from unittest.mock import MagicMock
import anthropic as anthropic_pkg
from ledger_one.categorize import categorize_transactions, fetch_business_patterns


MODEL = "claude-sonnet-5-5"


def _mock_json_response(anthropic, classifications: dict, stop_reason="end_turn", text=None):
    block = MagicMock()
    block.type = "text"
    block.text = text if text is not None else json.dumps({"classifications": [
        {"id": k, "category": v} for k, v in classifications.items()]})
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = stop_reason
    anthropic.beta.messages.create.return_value = resp


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
        model=MODEL,
    )
    assert results["t1"] == ("Coffee", "override")
    anthropic.beta.messages.create.assert_not_called()


def test_learned_wins_when_no_override(db):
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('whole foods mkt', 'Groceries')")
    anthropic = MagicMock()
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "whole foods mkt", "description": "WHOLE FOODS"}],
        categories=["Groceries", "Restaurants"],
        anthropic_client=anthropic,
        model=MODEL,
    )
    assert results["t1"] == ("Groceries", "learned")
    anthropic.beta.messages.create.assert_not_called()


def test_ai_fallback_for_unknown(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "Coffee"})
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "some novel merchant", "description": "SOME NOVEL"}],
        categories=["Coffee", "Restaurants"],
        anthropic_client=anthropic,
        model=MODEL,
    )
    assert results["t1"] == ("Coffee", "ai")
    anthropic.beta.messages.create.assert_called_once()


def test_ai_api_error_falls_back_to_uncategorized(db):
    anthropic = MagicMock()
    anthropic.beta.messages.create.side_effect = anthropic_pkg.APIStatusError(
        message="boom", response=MagicMock(), body=None
    )
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "novel", "description": "X"}],
        categories=["Coffee"],
        anthropic_client=anthropic,
        model=MODEL,
    )
    assert results["t1"] == ("Uncategorized", "ai")


def test_ai_invalid_category_becomes_uncategorized(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "MadeUpCategory"})
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "novel", "description": "X"}],
        categories=["Coffee"],
        anthropic_client=anthropic,
        model=MODEL,
    )
    assert results["t1"] == ("Uncategorized", "ai")


def test_exclude_categories_skips_override_and_uses_learned(db):
    db.execute("INSERT INTO category_overrides (merchant_pattern, category) "
               "VALUES ('dayton express', 'Business Expense')")
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('dayton express', 'Travel')")
    anthropic = MagicMock()
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "dayton express", "description": "DAYTON EXPRESS"}],
        categories=["Travel", "Restaurants"],
        anthropic_client=anthropic,
        model=MODEL,
        exclude_categories={"Business Expense"},
    )
    assert results["t1"] == ("Travel", "learned")
    anthropic.beta.messages.create.assert_not_called()


def test_exclude_categories_skips_override_and_learned_falls_to_ai(db):
    db.execute("INSERT INTO category_overrides (merchant_pattern, category) "
               "VALUES ('dayton express', 'Business Expense')")
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('dayton express', 'Business Expense')")
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "Travel"})
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "dayton express", "description": "DAYTON EXPRESS"}],
        categories=["Travel", "Restaurants"],
        anthropic_client=anthropic,
        model=MODEL,
        exclude_categories={"Business Expense"},
    )
    assert results["t1"] == ("Travel", "ai")
    anthropic.beta.messages.create.assert_called_once()


def test_exclude_categories_default_keeps_override(db):
    db.execute("INSERT INTO category_overrides (merchant_pattern, category) "
               "VALUES ('dayton express', 'Business Expense')")
    anthropic = MagicMock()
    results = categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "dayton express", "description": "DAYTON EXPRESS"}],
        categories=["Travel"],
        anthropic_client=anthropic,
        model=MODEL,
    )
    assert results["t1"] == ("Business Expense", "override")


def test_fetch_business_patterns(db):
    db.execute("INSERT INTO category_overrides (merchant_pattern, category, business) VALUES "
               "('dayton express', 'Travel', true), "
               "('courtyard dayton', 'Travel', true), "
               "('starbucks', 'Coffee', false)")
    assert fetch_business_patterns(
        db, ["dayton express", "starbucks", "unknown merchant"]
    ) == {"dayton express"}
    assert fetch_business_patterns(db, []) == set()


def _classify_one(db, anthropic, model=MODEL, categories=("Coffee", "Restaurants")):
    return categorize_transactions(
        db,
        [{"id": "t1", "merchant_pattern": "novel", "description": "NOVEL CAFE", "amount": -4.5},
         {"id": "t2", "merchant_pattern": "other", "description": "OTHER", "amount": -9}],
        categories=list(categories),
        anthropic_client=anthropic,
        model=model,
    )


def test_ai_request_uses_structured_output_with_category_enum(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "Coffee", "t2": "Restaurants"})
    assert _classify_one(db, anthropic) == {"t1": ("Coffee", "ai"), "t2": ("Restaurants", "ai")}
    kwargs = anthropic.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == MODEL
    assert "tools" not in kwargs and "tool_choice" not in kwargs
    item = kwargs["output_config"]["format"]["schema"]["properties"]["classifications"]["items"]
    assert item["properties"]["category"]["enum"] == ["Coffee", "Restaurants"]
    assert kwargs["output_config"]["effort"] == "medium"
    # Refused requests re-run on a fallback model server-side instead of failing the batch.
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["extra_body"] == {"fallbacks": "default"}


def test_haiku_skips_effort_and_fallbacks(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "Coffee", "t2": "Coffee"})
    _classify_one(db, anthropic, model="claude-haiku-4-5-20251001")
    kwargs = anthropic.beta.messages.create.call_args.kwargs
    assert "effort" not in kwargs["output_config"]
    assert "format" in kwargs["output_config"]
    assert "betas" not in kwargs and "extra_body" not in kwargs


def test_refusal_marks_batch_uncategorized(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "Coffee", "t2": "Coffee"}, stop_reason="refusal")
    assert _classify_one(db, anthropic) == {
        "t1": ("Uncategorized", "ai"), "t2": ("Uncategorized", "ai")}


def test_truncated_output_marks_batch_uncategorized(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {}, stop_reason="max_tokens", text='{"classifications": [{"id": "t1", "cat')
    assert _classify_one(db, anthropic) == {
        "t1": ("Uncategorized", "ai"), "t2": ("Uncategorized", "ai")}


def test_unparseable_output_marks_batch_uncategorized(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {}, text="not json")
    assert _classify_one(db, anthropic) == {
        "t1": ("Uncategorized", "ai"), "t2": ("Uncategorized", "ai")}


def test_id_missing_from_answer_is_uncategorized(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "Coffee"})
    assert _classify_one(db, anthropic) == {"t1": ("Coffee", "ai"), "t2": ("Uncategorized", "ai")}


def test_system_prompt_no_longer_mentions_a_tool(db):
    anthropic = MagicMock()
    _mock_json_response(anthropic, {"t1": "Coffee", "t2": "Coffee"})
    _classify_one(db, anthropic)
    system = anthropic.beta.messages.create.call_args.kwargs["system"][0]["text"]
    assert "tool" not in system
    assert "- Coffee" in system and "- Restaurants" in system


def test_ai_requests_are_batched(db):
    from ledger_one.categorize import BATCH_SIZE
    anthropic = MagicMock()
    _mock_json_response(anthropic, {})
    txns = [{"id": f"t{i}", "merchant_pattern": f"m{i}", "description": "X", "amount": -1}
            for i in range(BATCH_SIZE + 1)]
    categorize_transactions(db, txns, categories=["Coffee"], anthropic_client=anthropic, model=MODEL)
    sizes = [c.kwargs["messages"][0]["content"].count("<tx ")
             for c in anthropic.beta.messages.create.call_args_list]
    assert sizes == [BATCH_SIZE, 1]
