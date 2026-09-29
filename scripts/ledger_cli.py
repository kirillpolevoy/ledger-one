#!/usr/bin/env python3
"""ledger: small CLI for category overrides."""
import argparse
import os
import sys
from typing import NamedTuple
import psycopg
from dotenv import load_dotenv
from ledger_one.normalize import normalize_merchant


class OverrideResult(NamedTuple):
    pattern: str
    recategorized: int  # non-manual rows moved to the override's category
    manual_kept: int  # rows the user set by hand to a different category — left alone
    business_tagged: int  # rows newly tagged business (0 unless business=True)


def add_override(db, raw_pattern: str, category: str, *, business: bool = False) -> OverrideResult:
    """Upsert the override and apply it to past transactions.

    Manual wins: the retroactive category UPDATE skips rows the user set by
    hand. With business=True, past rows from the merchant are tagged business;
    business is never unset retroactively.
    """
    pattern = normalize_merchant(raw_pattern)
    with db.transaction(), db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO category_overrides (merchant_pattern, category, business)
            VALUES (%s, %s, %s)
            ON CONFLICT (merchant_pattern) DO UPDATE
              SET category = EXCLUDED.category, business = EXCLUDED.business
            """,
            (pattern, category, business),
        )
        cur.execute(
            "UPDATE transactions SET category = %s, categorization_source = 'override' "
            "WHERE merchant_pattern = %s AND category IS DISTINCT FROM %s "
            "AND categorization_source IS DISTINCT FROM 'manual'",
            (category, pattern, category),
        )
        recategorized = cur.rowcount
        cur.execute(
            "SELECT count(*) FROM transactions WHERE merchant_pattern = %s "
            "AND category IS DISTINCT FROM %s AND categorization_source = 'manual'",
            (pattern, category),
        )
        manual_kept = cur.fetchone()[0]
        business_tagged = 0
        if business:
            cur.execute(
                "UPDATE transactions SET business = true "
                "WHERE merchant_pattern = %s AND NOT business",
                (pattern,),
            )
            business_tagged = cur.rowcount
    return OverrideResult(pattern, recategorized, manual_kept, business_tagged)


def list_overrides(db) -> list[tuple[str, str, bool]]:
    return [
        (r[0], r[1], r[2]) for r in
        db.execute(
            "SELECT merchant_pattern, category, business FROM category_overrides "
            "ORDER BY merchant_pattern"
        ).fetchall()
    ]


def format_override(pattern: str, category: str, business: bool) -> str:
    return f"{pattern} → {category}" + (" [business]" if business else "")


def remove_override(db, raw_pattern: str) -> None:
    pattern = normalize_merchant(raw_pattern)
    db.execute("DELETE FROM category_overrides WHERE merchant_pattern = %s", (pattern,))


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(prog="ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)
    override = sub.add_parser("override")
    osub = override.add_subparsers(dest="subcmd", required=True)
    add = osub.add_parser("add"); add.add_argument("pattern"); add.add_argument("category")
    add.add_argument("--business", action="store_true",
                     help="Also tag this merchant's charges as business (past and future).")
    osub.add_parser("list")
    rm = osub.add_parser("remove"); rm.add_argument("pattern")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        if args.cmd == "override" and args.subcmd == "add":
            r = add_override(conn, args.pattern, args.category, business=args.business)
            print(f"Normalized pattern: {r.pattern!r}")
            print(f"Added override{' (business)' if args.business else ''}. "
                  f"Retroactively recategorized {r.recategorized} transactions.")
            if r.manual_kept:
                print(f"Kept {r.manual_kept} manually categorized transactions as they were.")
            if args.business:
                print(f"Tagged {r.business_tagged} transactions as business.")
            matched = conn.execute(
                "SELECT 1 FROM transactions WHERE merchant_pattern = %s LIMIT 1", (r.pattern,)
            ).fetchone()
            if not matched:
                pattern = r.pattern
                # Check if there are similar patterns the user might have meant
                similar = conn.execute(
                    "SELECT DISTINCT merchant_pattern FROM transactions "
                    "WHERE merchant_pattern LIKE %s LIMIT 5",
                    (f"%{pattern.split()[0]}%" if pattern else "",),
                ).fetchall()
                if similar:
                    print("Warning: no existing transactions matched this pattern.")
                    print("Similar patterns in your data:")
                    for (s,) in similar:
                        print(f"  {s!r}")
                else:
                    print("Note: no existing transactions matched. Override will apply to future pulls.")
        elif args.cmd == "override" and args.subcmd == "list":
            for p, c, b in list_overrides(conn):
                print(format_override(p, c, b))
        elif args.cmd == "override" and args.subcmd == "remove":
            remove_override(conn, args.pattern)
            print("Removed.")


if __name__ == "__main__":
    sys.exit(main() or 0)
