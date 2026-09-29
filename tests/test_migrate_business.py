import copy
import json
import re
from unittest.mock import MagicMock

import anthropic as anthropic_pkg
import pytest

from ledger_one import migrate_business
from ledger_one.migrate_business import (
    BUSINESS_EXPENSE,
    format_report,
    migrate_business_expense,
)

CATEGORIES = ["Travel", "Transportation", "Restaurants", "Coffee", "Income", "Transfers",
              "Uncategorized"]

_TX_RE = re.compile(r'<tx id="([^"]+)"[^>]*><desc>(.*?)</desc></tx>')


def _fake_anthropic(by_desc: dict[str, str]):
    """Same tool_use response shape as tests/test_categorize.py, but answers per
    description so one client can serve several classify calls."""
    client = MagicMock()

    def create(**kwargs):
        content = kwargs["messages"][0]["content"]
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": {
            tx_id: by_desc.get(desc, "Uncategorized") for tx_id, desc in _TX_RE.findall(content)
        }}
        resp = MagicMock()
        resp.content = [block]
        resp.stop_reason = "tool_use"
        return resp

    client.messages.create.side_effect = create
    return client


def _failing_anthropic():
    client = MagicMock()
    client.messages.create.side_effect = anthropic_pkg.APIStatusError(
        message="overloaded", response=MagicMock(), body=None
    )
    return client


def _no_ai():
    """A client that must never be called — --apply works from the plan alone."""
    client = MagicMock()
    client.messages.create.side_effect = AssertionError("AI called during a plan run")
    return client


AI_ANSWERS = {
    "UBER TRIP": "Transportation",
    "DAYTON EXPRESS": "Travel",
    "courtyard dayton": "Travel",  # synthetic tx for the override with no rows
}


def _tx(db, tx_id, desc, pattern, category, source, *, business=False, pending=False):
    db.execute(
        "INSERT INTO transactions (id, account_id, amount, description, merchant_pattern, "
        "category, posted_at, categorization_source, categorized_at, business, pending) "
        "VALUES (%s, 'a1', -25, %s, %s, %s, now(), %s, '2026-01-01', %s, %s)",
        (tx_id, desc, pattern, category, source, business, pending),
    )


def _account(db):
    db.execute("INSERT INTO accounts (id, name) VALUES ('a1', 'Visa')")


def _seed(db):
    _account(db)
    # Manually picked Business Expense on an Uber — the core bug this migration fixes.
    _tx(db, "t1", "UBER TRIP", "uber trip", BUSINESS_EXPENSE, "manual")
    # "Always business" override with past rows.
    _tx(db, "t2", "DAYTON EXPRESS", "dayton express", BUSINESS_EXPENSE, "override")
    _tx(db, "t3", "DAYTON EXPRESS", "dayton express", BUSINESS_EXPENSE, "override", pending=True)
    # Same merchant, not Business Expense (user moved it by hand) — keeps its
    # category, but the converted override tags it business.
    _tx(db, "t4", "DAYTON EXPRESS", "dayton express", "Restaurants", "manual")
    # Learned mapping already points somewhere else — categorizer uses it.
    _tx(db, "t5", "HILTON GARDEN INN", "hilton garden inn", BUSINESS_EXPENSE, "ai")
    # Unrelated personal row.
    _tx(db, "t6", "STARBUCKS", "starbucks", "Coffee", "learned")
    db.execute("""
        INSERT INTO category_overrides (merchant_pattern, category) VALUES
          ('dayton express', 'Business Expense'),
          ('courtyard dayton', 'Business Expense'),
          ('starbucks', 'Coffee');
        INSERT INTO merchant_categories (merchant_pattern, category) VALUES
          ('uber trip', 'Business Expense'),
          ('dayton express', 'Business Expense'),
          ('hilton garden inn', 'Travel'),
          ('old hotel', 'Business Expense'),
          ('starbucks', 'Coffee');
    """)


def _snapshot(db):
    return (
        db.execute("SELECT id, category, categorization_source, categorized_at, business "
                   "FROM transactions ORDER BY id").fetchall(),
        db.execute("SELECT merchant_pattern, category, business FROM category_overrides "
                   "ORDER BY merchant_pattern").fetchall(),
        db.execute("SELECT merchant_pattern, category, last_updated FROM merchant_categories "
                   "ORDER BY merchant_pattern").fetchall(),
    )


def _learned(db):
    return dict(db.execute("SELECT merchant_pattern, category FROM merchant_categories").fetchall())


def _dry(db, client=None, *, categories=CATEGORIES):
    """Dry run without a plan: derives categories (AI) and returns the report,
    whose "plan" is what --apply --plan would commit."""
    return migrate_business_expense(
        db, categories=categories,
        anthropic_client=client or _fake_anthropic(AI_ANSWERS),
        model="m", apply=False,
    )


def _apply(db, plan, *, categories=CATEGORIES, allow_uncategorized=False):
    return migrate_business_expense(
        db, categories=categories, anthropic_client=_no_ai(), model="m",
        apply=True, plan=json.loads(json.dumps(plan)),  # as if read back from the plan file
        allow_uncategorized=allow_uncategorized,
    )


def _dry_then_apply(db, client=None, **kw):
    return _apply(db, _dry(db, client)["plan"], **kw)


# --- dry run and the plan ------------------------------------------------------

def test_dry_run_reports_but_leaves_db_unchanged(db):
    _seed(db)
    before = _snapshot(db)
    report = _dry(db)
    assert _snapshot(db) == before
    assert report["applied"] is False
    assert report["counts"]["rows_migrated"] == 4
    assert report["counts"]["overrides_converted"] == 2
    # Trigger effects are real inside the rolled-back transaction, so the
    # report sees exactly what --apply would leave behind.
    assert report["learned_deleted"] == ["old hotel"]


def test_dry_run_plan_records_every_row_and_override(db):
    _seed(db)
    plan = _dry(db)["plan"]
    assert plan == {
        "rows": {
            "t1": {"category": "Transportation", "source": "ai", "description": "UBER TRIP"},
            "t2": {"category": "Travel", "source": "ai", "description": "DAYTON EXPRESS"},
            "t3": {"category": "Travel", "source": "ai", "description": "DAYTON EXPRESS"},
            "t5": {"category": "Travel", "source": "learned", "description": "HILTON GARDEN INN"},
        },
        "overrides": {"courtyard dayton": "Travel", "dayton express": "Travel"},
    }
    assert json.loads(json.dumps(plan)) == plan  # plain JSON, round-trips through the file


def test_apply_requires_a_plan(db):
    _seed(db)
    before = _snapshot(db)
    client = _fake_anthropic(AI_ANSWERS)
    with pytest.raises(ValueError, match="plan"):
        migrate_business_expense(db, categories=CATEGORIES, anthropic_client=client,
                                 model="m", apply=True)
    assert _snapshot(db) == before
    client.messages.create.assert_not_called()


def test_apply_commits_the_plan_without_calling_ai(db):
    _seed(db)
    report = _dry_then_apply(db)  # _apply uses a client that fails if called
    assert report["applied"] is True

    rows = {
        r[0]: r[1:] for r in db.execute(
            "SELECT id, category, categorization_source, business, categorized_at > '2026-01-02' "
            "FROM transactions"
        ).fetchall()
    }
    assert rows == {
        "t1": ("Transportation", "ai", True, True),
        "t2": ("Travel", "ai", True, True),
        "t3": ("Travel", "ai", True, True),
        "t4": ("Restaurants", "manual", True, False),  # tagged by override, category untouched
        "t5": ("Travel", "learned", True, True),
        "t6": ("Coffee", "learned", False, False),
    }
    assert db.execute(
        "SELECT merchant_pattern, category, business FROM category_overrides ORDER BY 1"
    ).fetchall() == [
        ("courtyard dayton", "Travel", True),
        ("dayton express", "Travel", True),
        ("starbucks", "Coffee", False),
    ]
    assert _learned(db) == {
        "dayton express": "Travel",
        "hilton garden inn": "Travel",
        "starbucks": "Coffee",
        "uber trip": "Transportation",
    }
    for table in ("transactions", "category_overrides", "merchant_categories"):
        assert db.execute(
            f"SELECT count(*) FROM {table} WHERE category = %s", (BUSINESS_EXPENSE,)
        ).fetchone() == (0,)

    assert report["learned_deleted"] == ["old hotel"]
    assert report["counts"] == {
        "rows_migrated": 4,
        "overrides_converted": 2,
        "business_tagged_by_overrides": 1,
        "learned_restored": 0,  # hilton garden inn: the migration re-picked Travel anyway
        "learned_set": 2,
        "learned_deleted": 1,
        "new_categories": {"Travel": 3, "Transportation": 1},
    }


def test_apply_honors_a_hand_edited_plan(db):
    """The operator fixes what the AI got wrong in the plan file; --apply
    commits exactly that."""
    _seed(db)
    plan = _dry(db)["plan"]
    plan["rows"]["t1"]["category"] = "Travel"
    plan["rows"]["t1"]["source"] = "manual"
    plan["overrides"]["dayton express"] = "Restaurants"
    _apply(db, plan)
    assert db.execute(
        "SELECT category, categorization_source, business FROM transactions WHERE id = 't1'"
    ).fetchone() == ("Travel", "manual", True)
    assert db.execute(
        "SELECT category, business FROM category_overrides WHERE merchant_pattern = 'dayton express'"
    ).fetchone() == ("Restaurants", True)


@pytest.mark.parametrize("change", [
    "INSERT INTO transactions (id, account_id, amount, description, merchant_pattern, category, "
    "posted_at) VALUES ('t9', 'a1', -9, 'DAYTON EXPRESS', 'dayton express', 'Business Expense', now())",
    "UPDATE transactions SET category = 'Travel', categorization_source = 'manual' WHERE id = 't1'",
    "INSERT INTO category_overrides (merchant_pattern, category) VALUES ('new inn', 'Business Expense')",
])
def test_apply_aborts_when_data_changed_since_the_dry_run(db, change):
    _seed(db)
    plan = _dry(db)["plan"]
    db.execute(change)
    before = _snapshot(db)
    with pytest.raises(ValueError, match="re-run the dry run"):
        _apply(db, plan)
    assert _snapshot(db) == before


def test_apply_rejects_a_plan_category_not_in_the_categories_file(db):
    _seed(db)
    plan = _dry(db)["plan"]
    plan["rows"]["t1"]["category"] = "Groceries"
    before = _snapshot(db)
    with pytest.raises(ValueError, match="Groceries"):
        _apply(db, plan)
    assert _snapshot(db) == before


def test_apply_rejects_a_malformed_plan(db):
    _seed(db)
    before = _snapshot(db)
    with pytest.raises(ValueError, match="plan"):
        _apply(db, {"rows": [], "overrides": {}})
    assert _snapshot(db) == before


def test_apply_rejects_an_unknown_source(db):
    """A hand-edited source is written as-is, so a typo like 'Manual' would
    slip past manual-wins (`IS DISTINCT FROM 'manual'`) later."""
    _seed(db)
    plan = _dry(db)["plan"]
    plan["rows"]["t1"]["source"] = "Manual"
    before = _snapshot(db)
    with pytest.raises(ValueError, match="Manual"):
        _apply(db, plan)
    assert _snapshot(db) == before


def test_plan_preview_checks_the_plan_without_ai_or_writes(db):
    """--plan without --apply: same checks as --apply, then rolls back."""
    _seed(db)
    plan = _dry(db)["plan"]
    before = _snapshot(db)
    report = migrate_business_expense(db, categories=CATEGORIES, anthropic_client=_no_ai(),
                                      model="m", apply=False, plan=plan)
    assert (report["applied"], report["from_plan"]) == (False, True)
    assert _snapshot(db) == before
    assert "The plan checks out" in "\n".join(format_report(report))

    plan["overrides"]["courtyard dayton"] = "Uncategorized"
    with pytest.raises(ValueError, match="courtyard dayton"):
        migrate_business_expense(db, categories=CATEGORIES, anthropic_client=_no_ai(),
                                 model="m", apply=False, plan=plan)
    assert _snapshot(db) == before


# --- Uncategorized guard (AI failure / no fit) ---------------------------------

def test_ai_failure_plans_uncategorized_and_apply_refuses_it(db):
    """An API outage must not leave rows/overrides/learned stuck in
    Uncategorized: the dry run flags them, --apply refuses the plan."""
    _seed(db)
    report = _dry(db, _failing_anthropic())
    assert report["plan"]["rows"]["t1"]["category"] == "Uncategorized"
    assert report["plan"]["overrides"]["courtyard dayton"] == "Uncategorized"
    assert report["problems"]
    text = "\n".join(format_report(report))
    assert "Uncategorized" in text and "courtyard dayton" in text

    before = _snapshot(db)
    with pytest.raises(ValueError, match="Uncategorized"):
        _apply(db, report["plan"])
    assert _snapshot(db) == before


def test_uncategorized_rows_need_allow_uncategorized_and_are_never_learned(db):
    _seed(db)
    plan = _dry(db)["plan"]
    plan["rows"]["t1"]["category"] = "Uncategorized"  # 'uber trip' learned was Business Expense
    before = _snapshot(db)
    with pytest.raises(ValueError, match="allow-uncategorized"):
        _apply(db, plan)
    assert _snapshot(db) == before

    report = _apply(db, plan, allow_uncategorized=True)
    assert db.execute("SELECT category, business FROM transactions WHERE id = 't1'").fetchone() == (
        "Uncategorized", True,
    )
    # The trigger learned Uncategorized from t1; the migration takes it back out
    # so the next Uber goes to AI instead of sticking in Uncategorized.
    assert "uber trip" not in _learned(db)
    assert "uber trip" in report["learned_deleted"]


def test_override_is_never_set_to_uncategorized(db):
    _seed(db)
    plan = _dry(db)["plan"]
    plan["overrides"]["courtyard dayton"] = "Uncategorized"
    before = _snapshot(db)
    with pytest.raises(ValueError, match="courtyard dayton"):
        _apply(db, plan, allow_uncategorized=True)
    assert _snapshot(db) == before


# --- learned mappings (merchant_categories) --------------------------------------

def test_learned_mapping_is_the_majority_not_the_last_row(db):
    """The learn trigger keeps whichever migrated row has the highest id; one
    misclassified row must not decide the mapping for every future charge."""
    _account(db)
    _tx(db, "u1", "UNITED 1", "united", BUSINESS_EXPENSE, "manual")
    _tx(db, "u2", "UNITED 2", "united", BUSINESS_EXPENSE, "manual")
    _tx(db, "u3", "UNITED 3", "united", BUSINESS_EXPENSE, "manual")  # highest id
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('united', 'Business Expense')")
    client = _fake_anthropic({"UNITED 1": "Travel", "UNITED 2": "Travel", "UNITED 3": "Transfers"})
    report = _dry_then_apply(db, client)
    assert _learned(db) == {"united": "Travel"}
    assert report["learned"] == [{"merchant_pattern": "united", "action": "set", "category": "Travel"}]


def test_learned_majority_skips_uncategorized_and_breaks_ties_alphabetically(db):
    _account(db)
    for tx_id in ("a1x", "a2x", "a3x"):
        _tx(db, tx_id, tx_id.upper(), "cafe", BUSINESS_EXPENSE, "manual")
    plan = _dry(db)["plan"]
    plan["rows"]["a1x"]["category"] = "Uncategorized"
    plan["rows"]["a2x"]["category"] = "Restaurants"
    plan["rows"]["a3x"]["category"] = "Travel"  # highest id — what the trigger alone would keep
    _apply(db, plan, allow_uncategorized=True)
    assert _learned(db) == {"cafe": "Restaurants"}


def test_valid_learned_mapping_is_restored(db):
    """A merchant whose learned mapping was a real category keeps it, even when
    the migration files its old Business Expense rows elsewhere."""
    _seed(db)
    stamp = db.execute(
        "SELECT last_updated FROM merchant_categories WHERE merchant_pattern = 'hilton garden inn'"
    ).fetchone()
    plan = _dry(db)["plan"]
    plan["rows"]["t5"]["category"] = "Restaurants"
    report = _apply(db, plan)
    assert db.execute(
        "SELECT category, last_updated FROM merchant_categories "
        "WHERE merchant_pattern = 'hilton garden inn'"
    ).fetchone() == ("Travel", stamp[0])
    assert {"merchant_pattern": "hilton garden inn", "action": "restored",
            "category": "Travel"} in report["learned"]


def test_learned_mapping_without_a_real_category_is_deleted(db):
    """No learned row before, and the only migrated row is Uncategorized: the
    trigger's Uncategorized mapping is removed rather than kept."""
    _account(db)
    _tx(db, "m1", "MYSTERY LLC", "mystery llc", BUSINESS_EXPENSE, "manual")
    report = _dry_then_apply(db, allow_uncategorized=True)  # AI has no answer → Uncategorized
    assert _learned(db) == {}
    assert report["learned"] == [
        {"merchant_pattern": "mystery llc", "action": "deleted", "category": None},
    ]


# --- overrides ------------------------------------------------------------------

def test_override_without_rows_is_classified_via_synthetic_tx(db):
    db.execute("INSERT INTO category_overrides (merchant_pattern, category) "
               "VALUES ('courtyard dayton', 'Business Expense')")
    client = _fake_anthropic(AI_ANSWERS)
    report = _dry(db, client)
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert 'amount="-1"><desc>courtyard dayton</desc>' in content
    assert report["overrides"] == [
        {"merchant_pattern": "courtyard dayton", "category": "Travel", "basis": "pattern"},
    ]
    report = _apply(db, report["plan"])
    assert report["counts"]["rows_migrated"] == 0
    assert db.execute("SELECT category, business FROM category_overrides").fetchall() == [
        ("Travel", True),
    ]


def test_override_category_is_most_common_among_its_rows(db):
    _account(db)
    _tx(db, "a", "DAYTON EXPRESS", "dayton express", BUSINESS_EXPENSE, "override")
    _tx(db, "b", "DAYTON EXPRESS DINER", "dayton express", BUSINESS_EXPENSE, "override")
    _tx(db, "c", "DAYTON EXPRESS DINER", "dayton express", BUSINESS_EXPENSE, "override")
    db.execute("INSERT INTO category_overrides (merchant_pattern, category) "
               "VALUES ('dayton express', 'Business Expense')")
    client = _fake_anthropic({"DAYTON EXPRESS": "Travel", "DAYTON EXPRESS DINER": "Restaurants"})
    report = _dry_then_apply(db, client)
    assert db.execute("SELECT category, business FROM category_overrides").fetchall() == [
        ("Restaurants", True),
    ]
    assert report["overrides"] == [
        {"merchant_pattern": "dayton express", "category": "Restaurants", "basis": "plan"},
    ]


def test_override_majority_skips_uncategorized_rows(db):
    """Rows the AI couldn't place don't outvote the one it could."""
    _account(db)
    _tx(db, "a", "DAYTON EXPRESS", "dayton express", BUSINESS_EXPENSE, "override")
    _tx(db, "b", "DX ???", "dayton express", BUSINESS_EXPENSE, "override")
    _tx(db, "c", "DX ???", "dayton express", BUSINESS_EXPENSE, "override")
    db.execute("INSERT INTO category_overrides (merchant_pattern, category) "
               "VALUES ('dayton express', 'Business Expense')")
    report = _dry(db, _fake_anthropic({"DAYTON EXPRESS": "Travel"}))
    assert report["plan"]["overrides"] == {"dayton express": "Travel"}
    assert report["overrides"][0]["basis"] == "rows"


def test_refuses_while_business_expense_is_still_configured(db):
    _seed(db)
    before = _snapshot(db)
    client = _fake_anthropic(AI_ANSWERS)
    with pytest.raises(ValueError, match="Business Expense"):
        _dry(db, client, categories=CATEGORIES + [BUSINESS_EXPENSE])
    assert _snapshot(db) == before
    client.messages.create.assert_not_called()


def test_rolls_back_when_business_expense_survives(db, monkeypatch):
    """Step g is the safety net: if anything still says Business Expense after
    the rewrite, the whole transaction rolls back."""
    _seed(db)
    before = _snapshot(db)
    monkeypatch.setattr(
        migrate_business, "categorize_transactions",
        lambda db, txns, **kw: {t["id"]: (BUSINESS_EXPENSE, "ai") for t in txns},
    )
    with pytest.raises(RuntimeError, match="Business Expense"):
        _dry(db)
    assert _snapshot(db) == before


# --- re-runs --------------------------------------------------------------------

def test_second_run_is_a_noop(db):
    _seed(db)
    _dry_then_apply(db)
    after_first = _snapshot(db)
    client = _fake_anthropic(AI_ANSWERS)
    report = _dry(db, client)
    assert report["plan"] == {"rows": {}, "overrides": {}}
    report = _apply(db, report["plan"])
    assert _snapshot(db) == after_first
    assert report["counts"]["rows_migrated"] == 0
    assert report["counts"]["overrides_converted"] == 0
    assert report["counts"]["business_tagged_by_overrides"] == 0
    client.messages.create.assert_not_called()


def test_rerun_tags_rows_pulled_by_old_code_after_the_first_apply(db):
    """If a pull from the pre-business code runs after --apply, it files the
    converted override's charges with business = false. A re-run repairs it."""
    _seed(db)
    _dry_then_apply(db)
    # What the old pull inserts for a new Dayton Express charge: override category, no business.
    _tx(db, "t7", "DAYTON EXPRESS", "dayton express", "Travel", "override")
    # Unrelated merchant without a business override stays personal.
    _tx(db, "t8", "STARBUCKS", "starbucks", "Coffee", "override")
    learned_before = _learned(db)

    report = _dry_then_apply(db)
    assert report["counts"]["business_tagged_by_overrides"] == 1
    assert dict(db.execute("SELECT id, business FROM transactions WHERE id IN ('t7', 't8')")
                .fetchall()) == {"t7": True, "t8": False}
    assert _learned(db) == learned_before  # tagging never touches learned mappings


# --- report formatting ----------------------------------------------------------

def test_format_report_marks_dry_run_and_points_at_the_plan(db):
    _seed(db)
    lines = format_report(_dry(db))
    text = "\n".join(lines)
    assert "DRY RUN" in text
    assert "t1" in text and "manual" in text and "Transportation" in text
    assert "dayton express" in text
    assert "--apply --plan" in text


def test_format_report_lists_learned_changes(db):
    _account(db)
    _tx(db, "u1", "UNITED 1", "united", BUSINESS_EXPENSE, "manual")
    db.execute("INSERT INTO merchant_categories (merchant_pattern, category) "
               "VALUES ('united', 'Business Expense')")
    text = "\n".join(format_report(_dry(db, _fake_anthropic({"UNITED 1": "Travel"}))))
    assert "united -> Travel (set)" in text


def test_format_report_warns_on_allowed_uncategorized_rows():
    report = {
        "applied": True,
        "from_plan": True,
        "rows": [{"id": "t1", "description": "X", "old_source": "ai",
                  "new_category": "Uncategorized", "new_source": "ai"}],
        "overrides": [],
        "learned": [{"merchant_pattern": "x", "action": "deleted", "category": None}],
        "learned_deleted": ["x"],
        "problems": [],
        "plan": {"rows": {"t1": {"category": "Uncategorized", "source": "ai",
                                 "description": "X"}}, "overrides": {}},
        "counts": {"rows_migrated": 1, "overrides_converted": 0,
                   "business_tagged_by_overrides": 0, "learned_restored": 0,
                   "learned_set": 0, "learned_deleted": 1,
                   "new_categories": {"Uncategorized": 1}},
    }
    text = "\n".join(format_report(copy.deepcopy(report)))
    assert "Warning: 1 row(s) left in Uncategorized" in text
    assert "DRY RUN" not in text and "--apply" not in text
