#!/usr/bin/env python3
"""ledger: small CLI for category overrides."""
import argparse
import os
import sys
import psycopg
from dotenv import load_dotenv
from ledger_one.normalize import normalize_merchant


def add_override(db, raw_pattern: str, category: str) -> int:
    pattern = normalize_merchant(raw_pattern)
    db.execute(
        """
        INSERT INTO category_overrides (merchant_pattern, category) VALUES (%s, %s)
        ON CONFLICT (merchant_pattern) DO UPDATE SET category = EXCLUDED.category
        """,
        (pattern, category),
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE transactions SET category = %s, categorization_source = 'override' "
            "WHERE merchant_pattern = %s",
            (category, pattern),
        )
        return cur.rowcount


def list_overrides(db) -> list[tuple[str, str]]:
    return [
        (r[0], r[1]) for r in
        db.execute(
            "SELECT merchant_pattern, category FROM category_overrides ORDER BY merchant_pattern"
        ).fetchall()
    ]


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
    osub.add_parser("list")
    rm = osub.add_parser("remove"); rm.add_argument("pattern")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        if args.cmd == "override" and args.subcmd == "add":
            n = add_override(conn, args.pattern, args.category)
            print(f"Added override. Retroactively updated {n} transactions.")
        elif args.cmd == "override" and args.subcmd == "list":
            for p, c in list_overrides(conn):
                print(f"{p} \u2192 {c}")
        elif args.cmd == "override" and args.subcmd == "remove":
            remove_override(conn, args.pattern)
            print("Removed.")


if __name__ == "__main__":
    sys.exit(main() or 0)
