"""scripts/migrate_business_expense.py: dry run writes the plan, --apply --plan
commits it. Runs the real CLI entry point against the test DB with a fake AI."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import migrate_business_expense as cli  # noqa: E402

from test_migrate_business import AI_ANSWERS, _fake_anthropic, _seed, _snapshot  # noqa: E402


@pytest.fixture
def env(db_url, tmp_path, monkeypatch):
    # Never read the real .env in tests; the DB is the local test DB.
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: _fake_anthropic(AI_ANSWERS))
    cats = tmp_path / "categories.yaml"
    cats.write_text("categories:\n" + "".join(
        f"  - {c}\n" for c in ["Travel", "Transportation", "Restaurants", "Coffee"]
    ))
    return {"cats": str(cats), "plan": str(tmp_path / "plan.json"), "url": db_url}


def _no_client(**kw):
    raise AssertionError("--apply --plan must not build an AI client")


def test_dry_run_writes_the_plan_and_changes_nothing(db, env, capsys):
    _seed(db)
    before = _snapshot(db)
    assert cli.main(["--categories", env["cats"], "--plan-out", env["plan"]]) == 0
    assert _snapshot(db) == before
    plan = json.loads(Path(env["plan"]).read_text())
    assert set(plan["rows"]) == {"t1", "t2", "t3", "t5"}
    assert plan["overrides"] == {"courtyard dayton": "Travel", "dayton express": "Travel"}
    out = capsys.readouterr()
    assert "DRY RUN" in out.out
    assert f"--apply --plan {env['plan']}" in out.out
    assert env["url"] not in out.out + out.err


def test_apply_commits_the_edited_plan_without_ai(db, env, monkeypatch, capsys):
    _seed(db)
    assert cli.main(["--categories", env["cats"], "--plan-out", env["plan"]]) == 0
    plan = json.loads(Path(env["plan"]).read_text())
    plan["rows"]["t1"]["category"] = "Travel"  # operator's fix
    Path(env["plan"]).write_text(json.dumps(plan))
    capsys.readouterr()  # drop the dry run's output

    monkeypatch.delenv("ANTHROPIC_API_KEY")  # not needed when applying a plan
    monkeypatch.setattr(cli, "Anthropic", _no_client)
    assert cli.main(["--categories", env["cats"], "--apply", "--plan", env["plan"]]) == 0
    assert db.execute("SELECT category, business FROM transactions WHERE id = 't1'").fetchone() == (
        "Travel", True,
    )
    out = capsys.readouterr()
    assert "DRY RUN" not in out.out
    assert env["url"] not in out.out + out.err


def test_apply_without_plan_is_refused_before_connecting(env, monkeypatch, capsys):
    monkeypatch.setattr(cli.psycopg, "connect", _no_client)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--categories", env["cats"], "--apply"])
    assert exc.value.code == 2
    assert "--plan" in capsys.readouterr().err


def test_stale_plan_aborts_with_nothing_written(db, env, monkeypatch, capsys):
    _seed(db)
    assert cli.main(["--categories", env["cats"], "--plan-out", env["plan"]]) == 0
    db.execute("UPDATE transactions SET category = 'Travel' WHERE id = 't2'")
    before = _snapshot(db)
    monkeypatch.setattr(cli, "Anthropic", _no_client)
    assert cli.main(["--categories", env["cats"], "--apply", "--plan", env["plan"]]) == 1
    assert _snapshot(db) == before
    err = capsys.readouterr().err
    assert "nothing written" in err and "re-run the dry run" in err


def test_unreadable_plan_exits_nonzero(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.psycopg, "connect", _no_client)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cli.main(["--categories", env["cats"], "--apply", "--plan", str(bad)]) == 1
    assert "plan" in capsys.readouterr().err
