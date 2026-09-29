"""One-time data migration: retire the 'Business Expense' category.

Business used to be a category, so a merchant like Uber flip-flopped between
Transportation and Business Expense through the learn trigger and overrides.
Now category says what a charge is and `transactions.business` says who pays.
This moves every 'Business Expense' row, override, and learned mapping onto
the flag and gives each a real category.

Two passes, because the AI is not deterministic: a dry run derives the new
categories and returns them as a plan; the operator reviews (and may edit)
the plan; --apply commits exactly the plan, with no AI calls.
"""
import logging
from collections import Counter

import psycopg

from ledger_one.categorize import categorize_transactions
from ledger_one.config import UNCATEGORIZED

log = logging.getLogger(__name__)

BUSINESS_EXPENSE = "Business Expense"
PLAN_SOURCES = frozenset({"override", "learned", "ai", "manual"})


def migrate_business_expense(
    db, *, categories, anthropic_client, model, apply: bool,
    plan: dict | None = None, allow_uncategorized: bool = False,
) -> dict:
    """Convert 'Business Expense' into business = true plus a real category.

    Without `plan`, new categories are derived (overrides → learned → AI, with
    Business Expense excluded) and returned as report["plan"]:
    {"rows": {id: {"category", "source", "description"}},
     "overrides": {pattern: category}}. That is only allowed as a dry run.
    With `plan`, its categories are used as-is and the AI is never called; the
    plan must cover exactly the current Business Expense rows and overrides.

    Runs in one transaction. With apply=False every step still executes —
    including the learn trigger's writes — and is then rolled back, so the
    report shows exactly what apply=True would commit.

    Raises ValueError (nothing written) if 'Business Expense' is still in
    `categories`, if apply=True without a plan, or if the plan is malformed,
    stale, names a category not in `categories`, sets an override to
    Uncategorized, or sets a row to Uncategorized without allow_uncategorized.
    Raises RuntimeError if any 'Business Expense' survives the rewrite.
    """
    if BUSINESS_EXPENSE in categories:
        raise ValueError(
            f"Remove {BUSINESS_EXPENSE!r} from the categories file before migrating "
            "— it's a flag now, not a category."
        )
    if apply and plan is None:
        raise ValueError(
            "apply needs a plan from a dry run — the AI can answer differently each "
            "run, so only a reviewed plan gets committed."
        )
    if plan is not None:
        _check_plan_shape(plan)
    from_plan = plan is not None

    with db.transaction() as txn:
        # b. Rows and overrides to convert. FOR UPDATE holds off a concurrent
        #    pull's transition/reconcile writes on these rows until we commit.
        rows = db.execute(
            """
            SELECT id, description, merchant_pattern, amount, categorization_source
            FROM transactions WHERE category = %s
            ORDER BY posted_at, id
            FOR UPDATE
            """,
            (BUSINESS_EXPENSE,),
        ).fetchall()
        override_patterns = [
            r[0] for r in db.execute(
                "SELECT merchant_pattern FROM category_overrides WHERE category = %s "
                "ORDER BY merchant_pattern FOR UPDATE",
                (BUSINESS_EXPENSE,),
            ).fetchall()
        ]

        # c. New categories: from the reviewed plan, or derived for a new one.
        if from_plan:
            _check_plan_matches(plan, rows, override_patterns)
            override_basis = {p: "plan" for p in override_patterns}
        else:
            plan, override_basis = _derive_plan(
                db, rows, override_patterns,
                categories=categories, anthropic_client=anthropic_client, model=model,
            )
        problems = _plan_problems(plan, categories, allow_uncategorized=allow_uncategorized)
        if from_plan and problems:
            raise ValueError("plan rejected: " + "; ".join(problems))

        report_rows = [
            {
                "id": tx_id,
                "description": desc,
                "old_source": old_source,
                "new_category": plan["rows"][tx_id]["category"],
                "new_source": plan["rows"][tx_id]["source"],
            }
            for tx_id, desc, _pattern, _amount, old_source in rows
        ]

        # Learned mappings of the migrated merchants, before step d's UPDATE
        # fires the learn trigger over them.
        migrated_patterns = sorted({r[2] for r in rows if r[2]})
        learned_before = {
            r[0]: (r[1], r[2]) for r in db.execute(
                "SELECT merchant_pattern, category, last_updated FROM merchant_categories "
                "WHERE merchant_pattern = ANY(%s)",
                (migrated_patterns,),
            ).fetchall()
        }

        # d. One statement, so the learn trigger fires once for these merchants.
        db.execute(
            """
            UPDATE transactions t
            SET business = true,
                category = u.category,
                categorization_source = u.source,
                categorized_at = now()
            FROM UNNEST(%s::text[], %s::text[], %s::text[]) AS u(id, category, source)
            WHERE t.id = u.id
            """,
            (
                [r["id"] for r in report_rows],
                [r["new_category"] for r in report_rows],
                [r["new_source"] for r in report_rows],
            ),
        )

        # d2. Undo what the trigger just learned for those merchants. It keeps
        #     the highest-id row's category, so one misfiled row would decide
        #     every future charge. A valid mapping from before stays; otherwise
        #     the merchant gets its majority real category, or no mapping (next
        #     charge goes to AI) — never Uncategorized.
        learned_after = dict(db.execute(
            "SELECT merchant_pattern, category FROM merchant_categories "
            "WHERE merchant_pattern = ANY(%s)",
            (migrated_patterns,),
        ).fetchall())
        by_pattern: dict[str, Counter] = {}
        for tx_id, _desc, pattern, _amount, _src in rows:
            if pattern:
                by_pattern.setdefault(pattern, Counter())[plan["rows"][tx_id]["category"]] += 1
        report_learned = []
        for pattern in migrated_patterns:
            before = learned_before.get(pattern)
            if before and before[0] != BUSINESS_EXPENSE:
                _upsert_learned(db, pattern, *before)
                if learned_after.get(pattern) != before[0]:
                    report_learned.append(
                        {"merchant_pattern": pattern, "action": "restored", "category": before[0]}
                    )
            elif (category := _majority(by_pattern[pattern])) is not None:
                _upsert_learned(db, pattern, category, None)
                report_learned.append(
                    {"merchant_pattern": pattern, "action": "set", "category": category}
                )
            else:
                db.execute("DELETE FROM merchant_categories WHERE merchant_pattern = %s", (pattern,))
                report_learned.append(
                    {"merchant_pattern": pattern, "action": "deleted", "category": None}
                )

        # e. Overrides become "category X, always business".
        report_overrides = []
        for pattern in override_patterns:
            category = plan["overrides"][pattern]
            db.execute(
                "UPDATE category_overrides SET category = %s, business = true "
                "WHERE merchant_pattern = %s",
                (category, pattern),
            )
            report_overrides.append({
                "merchant_pattern": pattern,
                "category": category,
                "basis": override_basis[pattern],
            })

        # e2. Every business override tags its merchant's rows — not just the
        #     ones converted above. Idempotent, only ever adds, and it repairs
        #     rows a pre-business pull filed with business = false after an
        #     earlier run converted their override.
        with db.cursor() as cur:
            cur.execute(
                """
                UPDATE transactions t SET business = true
                FROM category_overrides o
                WHERE o.business AND t.merchant_pattern = o.merchant_pattern
                  AND NOT t.business
                """
            )
            business_tagged = cur.rowcount

        # f. Learned mappings still pointing at Business Expense (merchants with
        #    no migrated rows). Their next charge re-learns via the pull.
        for (pattern,) in db.execute(
            "DELETE FROM merchant_categories WHERE category = %s RETURNING merchant_pattern",
            (BUSINESS_EXPENSE,),
        ).fetchall():
            report_learned.append(
                {"merchant_pattern": pattern, "action": "deleted", "category": None}
            )

        # g. Safety net: nothing may still say Business Expense.
        remaining = db.execute(
            """
            SELECT
              (SELECT count(*) FROM transactions WHERE category = %(c)s),
              (SELECT count(*) FROM category_overrides WHERE category = %(c)s),
              (SELECT count(*) FROM merchant_categories WHERE category = %(c)s)
            """,
            {"c": BUSINESS_EXPENSE},
        ).fetchone()
        if any(remaining):
            raise RuntimeError(
                f"{BUSINESS_EXPENSE!r} still present after migration "
                f"(transactions={remaining[0]}, category_overrides={remaining[1]}, "
                f"merchant_categories={remaining[2]}); rolled back."
            )

        actions = Counter(entry["action"] for entry in report_learned)
        report = {
            "applied": apply,
            "from_plan": from_plan,
            "rows": report_rows,
            "overrides": report_overrides,
            "learned": report_learned,
            "learned_deleted": sorted(
                e["merchant_pattern"] for e in report_learned if e["action"] == "deleted"
            ),
            "problems": problems,
            "plan": plan,
            "counts": {
                "rows_migrated": len(report_rows),
                "overrides_converted": len(report_overrides),
                "business_tagged_by_overrides": business_tagged,
                "learned_restored": actions["restored"],
                "learned_set": actions["set"],
                "learned_deleted": actions["deleted"],
                "new_categories": dict(Counter(r["new_category"] for r in report_rows)),
            },
        }
        if not apply:
            raise psycopg.Rollback(txn)
    return report


def _derive_plan(db, rows, override_patterns, *, categories, anthropic_client, model):
    """Categories as if Business Expense never existed: overrides/learned that
    say Business Expense fall through to AI. An override takes the most common
    real category among its merchant's rows; a merchant with none is
    classified from its pattern through the same categorizer."""
    txns = [
        {"id": r[0], "description": r[1], "merchant_pattern": r[2], "amount": r[3]}
        for r in rows
    ]
    results = categorize_transactions(
        db, txns,
        categories=categories,
        anthropic_client=anthropic_client,
        model=model,
        exclude_categories={BUSINESS_EXPENSE},
    )
    by_pattern: dict[str, Counter] = {}
    for tx_id, _desc, pattern, _amount, _src in rows:
        by_pattern.setdefault(pattern, Counter())[results[tx_id][0]] += 1

    from_rows = {p: _majority(by_pattern.get(p, Counter())) for p in override_patterns}
    unmatched = [p for p in override_patterns if from_rows[p] is None]
    synthetic = [
        {"id": f"override-{i}", "description": p, "merchant_pattern": p, "amount": -1}
        for i, p in enumerate(unmatched)
    ]
    classified = categorize_transactions(
        db, synthetic,
        categories=categories,
        anthropic_client=anthropic_client,
        model=model,
        exclude_categories={BUSINESS_EXPENSE},
    )
    from_pattern = {s["merchant_pattern"]: classified[s["id"]][0] for s in synthetic}

    plan = {
        "rows": {
            tx_id: {
                "category": results[tx_id][0],
                "source": results[tx_id][1],
                "description": desc,
            }
            for tx_id, desc, _pattern, _amount, _src in rows
        },
        "overrides": {p: from_rows[p] or from_pattern[p] for p in override_patterns},
    }
    basis = {p: "rows" if from_rows[p] else "pattern" for p in override_patterns}
    return plan, basis


def _majority(counts: Counter) -> str | None:
    """Most common category other than Uncategorized; ties go to the
    alphabetically first. None when there is no real category."""
    ranked = sorted((c for c in counts if c != UNCATEGORIZED), key=lambda c: (-counts[c], c))
    return ranked[0] if ranked else None


def _upsert_learned(db, pattern, category, last_updated):
    db.execute(
        """
        INSERT INTO merchant_categories (merchant_pattern, category, last_updated)
        VALUES (%s, %s, COALESCE(%s, now()))
        ON CONFLICT (merchant_pattern) DO UPDATE
          SET category = EXCLUDED.category, last_updated = EXCLUDED.last_updated
        """,
        (pattern, category, last_updated),
    )


def _check_plan_shape(plan) -> None:
    ok = (
        isinstance(plan, dict)
        and isinstance(plan.get("rows"), dict)
        and isinstance(plan.get("overrides"), dict)
        and all(
            isinstance(v, dict)
            and isinstance(v.get("category"), str)
            and isinstance(v.get("source"), str)
            for v in plan["rows"].values()
        )
        and all(isinstance(v, str) for v in plan["overrides"].values())
    )
    if not ok:
        raise ValueError(
            'malformed plan: expected {"rows": {id: {"category": ..., "source": ...}}, '
            '"overrides": {pattern: category}} — re-run the dry run to regenerate it.'
        )


def _check_plan_matches(plan, rows, override_patterns) -> None:
    rows_now, rows_planned = {r[0] for r in rows}, set(plan["rows"])
    overrides_now, overrides_planned = set(override_patterns), set(plan["overrides"])
    if rows_now != rows_planned or overrides_now != overrides_planned:
        raise ValueError(
            "data changed since the dry run — re-run the dry run "
            f"(rows: {len(rows_now - rows_planned)} new, {len(rows_planned - rows_now)} gone; "
            f"overrides: {len(overrides_now - overrides_planned)} new, "
            f"{len(overrides_planned - overrides_now)} gone)."
        )


def _plan_problems(plan, categories, *, allow_uncategorized: bool) -> list[str]:
    """What would make --apply refuse this plan. Empty means it can be applied."""
    allowed = set(categories) | {UNCATEGORIZED}
    planned = {v["category"] for v in plan["rows"].values()} | set(plan["overrides"].values())
    problems = []
    if unknown := sorted(planned - allowed):
        problems.append(f"not in the categories file: {', '.join(unknown)}")
    if bad := sorted({v["source"] for v in plan["rows"].values()} - PLAN_SOURCES):
        problems.append(
            f"unknown source(s) {', '.join(bad)} — use one of {', '.join(sorted(PLAN_SOURCES))}"
        )
    if uncat := sorted(p for p, c in plan["overrides"].items() if c == UNCATEGORIZED):
        problems.append(
            f"override(s) set to Uncategorized: {', '.join(uncat)} — give each a real "
            "category (an Uncategorized business override drops its charges from every total)"
        )
    uncat_rows = sorted(i for i, v in plan["rows"].items() if v["category"] == UNCATEGORIZED)
    if uncat_rows and not allow_uncategorized:
        problems.append(
            f"{len(uncat_rows)} row(s) set to Uncategorized: {', '.join(uncat_rows)} — "
            "pick a category, or pass --allow-uncategorized to fix them by hand later"
        )
    return problems


_OVERRIDE_BASIS = {
    "rows": "most common among its rows",
    "pattern": "classified from pattern",
    "plan": "from plan",
}


def format_report(report: dict) -> list[str]:
    """Human-readable lines for the CLI. Contains ledger data only — no env values."""
    lines = []
    if not report["applied"]:
        lines.append("DRY RUN — every step ran, then rolled back. Nothing was written.")
    rows = report["rows"]
    lines.append(f"Transactions converted to business ({len(rows)}):")
    for r in rows:
        lines.append(
            f"  - {r['id']} | {(r['description'] or '')[:40]} | "
            f"{r['old_source']} -> {r['new_category']} ({r['new_source']})"
        )
    lines.append(f"Overrides converted ({len(report['overrides'])}):")
    for o in report["overrides"]:
        lines.append(
            f"  - {o['merchant_pattern']} -> {o['category']} [business] "
            f"({_OVERRIDE_BASIS[o['basis']]})"
        )
    lines.append(f"Learned mappings changed ({len(report['learned'])}):")
    for e in report["learned"]:
        if e["action"] == "deleted":
            lines.append(f"  - {e['merchant_pattern']} (deleted)")
        else:
            lines.append(f"  - {e['merchant_pattern']} -> {e['category']} ({e['action']})")
    counts = report["counts"]
    lines.append(
        "Counts: "
        f"rows_migrated={counts['rows_migrated']}, "
        f"overrides_converted={counts['overrides_converted']}, "
        f"business_tagged_by_overrides={counts['business_tagged_by_overrides']}, "
        f"learned_restored={counts['learned_restored']}, "
        f"learned_set={counts['learned_set']}, "
        f"learned_deleted={counts['learned_deleted']}"
    )
    if counts["new_categories"]:
        lines.append("New categories: " + ", ".join(
            f"{c}={n}" for c, n in sorted(counts["new_categories"].items())
        ))
    if report["problems"]:
        lines.append("Fix these in the plan before --apply:")
        lines.extend(f"  - {p}" for p in report["problems"])
    elif uncategorized := counts["new_categories"].get(UNCATEGORIZED, 0):
        lines.append(
            f"Warning: {uncategorized} row(s) left in Uncategorized (--allow-uncategorized). "
            "Business-spend totals skip Uncategorized — fix these by hand."
        )
    if not report["applied"]:
        if report["from_plan"]:
            lines.append("The plan checks out. Re-run with --apply --plan PLAN to commit it.")
        else:
            lines.append(
                "Review the plan (edit any category), then re-run with "
                "--apply --plan PLAN to commit exactly that."
            )
    return lines
