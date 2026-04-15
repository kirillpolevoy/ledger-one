# Extending ledger-one

`ledger-one` is the data layer only — no UI, no API, no opinions about how you query. The expected pattern is a **two-layer architecture**:

1. **Public layer** (`ledger-one` repo): schema + pull pipeline + CLI. Install once, pull updates.
2. **Private layer** (your repo): UI, dashboards, reports, custom analytics. Imports `ledger_one` as a library.

## Contract

The private layer:
- **Reads** freely from any table (`transactions`, `accounts`, `merchant_categories`).
- **Writes** only to `category_overrides` (via `scripts/ledger_cli.py override add` or direct SQL).
- **Updates** `transactions.category` when the user manually recategorizes — the DB trigger updates `merchant_categories` automatically.

## Example: private dashboard repo

```python
# private-dashboard/app.py
import os
import psycopg
from ledger_one.normalize import normalize_merchant  # reuse the public layer's normalizer

def top_merchants_this_month():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        return conn.execute("""
            SELECT merchant_pattern, SUM(-amount) AS spent
            FROM transactions
            WHERE posted_at >= date_trunc('month', now()) AND amount < 0
            GROUP BY merchant_pattern ORDER BY spent DESC LIMIT 10
        """).fetchall()

def recategorize(conn, tx_id: str, new_category: str):
    # The learn trigger will update merchant_categories automatically.
    conn.execute("UPDATE transactions SET category = %s WHERE id = %s",
                 (new_category, tx_id))
```

Install `ledger_one` into the private project with `pip install -e /path/to/ledger-one` or vendor it as a git submodule. Both layers share the same `DATABASE_URL`.

## Schema migrations

v1 has no migration framework. If `ledger-one` ships a v2 schema change, you'll apply it manually via SQL. The private layer should treat the schema as a stable contract and not depend on columns that aren't in `scripts/schema.sql`.
