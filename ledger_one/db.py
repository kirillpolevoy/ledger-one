import json
import psycopg


def upsert_accounts(db: psycopg.Connection, accounts: list[dict]) -> None:
    if not accounts:
        return
    rows = [
        (a["id"], a["name"], a.get("institution"), a.get("currency", "USD"),
         a.get("balance"), a.get("balance_date"))
        for a in accounts
    ]
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO accounts (id, name, institution, currency, last_balance, last_balance_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              name = EXCLUDED.name,
              institution = EXCLUDED.institution,
              currency = EXCLUDED.currency,
              last_balance = EXCLUDED.last_balance,
              last_balance_date = EXCLUDED.last_balance_date
            """,
            rows,
        )


def insert_transactions(db: psycopg.Connection, txns: list[dict]) -> int:
    if not txns:
        return 0
    rows = [
        (
            t["id"], t["account_id"], t["amount"], t["description"],
            t["merchant_pattern"], t["category"], t["posted_at"],
            json.dumps(t.get("raw_payload") or {}), t["source"],
        )
        for t in txns
    ]
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO transactions (
              id, account_id, amount, description, merchant_pattern,
              category, posted_at, raw_payload, categorized_at, categorization_source
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now(), %s)
            ON CONFLICT (id) DO NOTHING
            """,
            rows,
        )
        return cur.rowcount
