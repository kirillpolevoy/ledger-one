#!/usr/bin/env python3
"""One-time: turn the 'Business Expense' category into the `business` flag.

Order (see scripts/migrations/2026-09-28-add-business.sql):
  1. Apply the DDL migration.
  2. Deploy the new pull code (merge to main — the scheduled pull runs from
     main). An old pull after --apply files business-override charges as
     personal; re-running this script repairs that.
  3. Remove 'Business Expense' from the categories file.
  4. Dry run (the default): runs every step in one transaction, prints the
     report, rolls back, and writes the plan (--plan-out) — each row's and
     override's new category.
  5. Review the plan; edit any category the AI got wrong.
  6. --apply --plan PLAN: commits exactly the plan, with no AI calls. Aborts
     with nothing written if the data changed since the dry run.

Run outside the 18:00 UTC pull window. The plan file holds transaction
descriptions — keep it out of git.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import psycopg
from anthropic import Anthropic
from dotenv import load_dotenv

from ledger_one.config import load_categories
from ledger_one.migrate_business import format_report, migrate_business_expense

DEFAULT_PLAN = Path("business_migration_plan.json")


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--categories", type=Path, default=Path("config/categories.yaml"))
    parser.add_argument("--apply", action="store_true",
                        help="Commit the plan. Without it, the migration runs and rolls back.")
    parser.add_argument("--plan", type=Path,
                        help="Plan file from a dry run. Required with --apply; its categories "
                             "are used as-is, with no AI calls. Without --apply, previews it.")
    parser.add_argument("--plan-out", type=Path, default=DEFAULT_PLAN,
                        help="Where a dry run without --plan writes its plan "
                             "(default: %(default)s).")
    parser.add_argument("--allow-uncategorized", action="store_true",
                        help="Let plan rows stay Uncategorized (fix them by hand later). "
                             "Overrides can never be Uncategorized.")
    args = parser.parse_args(argv)
    if args.apply and not args.plan:
        parser.error("--apply requires --plan PLAN — run a dry run first and review the "
                     "plan it writes.")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    db_url = _require("DATABASE_URL")

    plan = None
    client = None
    if args.plan:
        try:
            plan = json.loads(args.plan.read_text())
        except (OSError, ValueError) as e:
            print(f"Can't read plan {args.plan}: {e}", file=sys.stderr)
            return 1
    else:
        _require("ANTHROPIC_API_KEY")
        client = Anthropic(max_retries=5)
    model = os.environ.get("LEDGER_CATEGORIZATION_MODEL", "claude-haiku-4-5-20251001")

    if not args.categories.exists():
        print(f"Missing {args.categories}. Copy config/categories.yaml.example.", file=sys.stderr)
        return 1
    categories = load_categories(args.categories)

    with psycopg.connect(db_url, autocommit=True) as conn:
        try:
            report = migrate_business_expense(
                conn, categories=categories,
                anthropic_client=client, model=model,
                apply=args.apply, plan=plan,
                allow_uncategorized=args.allow_uncategorized,
            )
        except (ValueError, RuntimeError) as e:
            print(f"Migration aborted, nothing written: {e}", file=sys.stderr)
            return 1
    # Never log or print DATABASE_URL, anywhere.
    for line in format_report(report):
        print(line)
    if plan is None:
        args.plan_out.write_text(json.dumps(report["plan"], indent=2, sort_keys=True) + "\n")
        print(f"Plan written to {args.plan_out}. Review it, then: "
              f"python scripts/migrate_business_expense.py --apply --plan {args.plan_out}")
    return 0


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"Missing env var: {name}", file=sys.stderr)
        sys.exit(1)
    return val


if __name__ == "__main__":
    sys.exit(main())
