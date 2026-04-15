# ledger-one

Pull bank transactions from SimpleFIN into your own Postgres, categorized with Claude. Replaces Mint / Copilot Money for people who want to own their data.

- **~$15/year** (SimpleFIN Bridge) + pennies in Claude API calls.
- **Your database**, your queries, your UI.
- Ships as an Anthropic skill — Claude walks you through setup.

## Architecture

A 3-tier categorization cascade:

1. **Explicit overrides** (`category_overrides` table) — user rules that always win.
2. **Learned patterns** (`merchant_categories` table) — built from your history, seeded optionally from a Copilot CSV export.
3. **Claude Haiku** — fallback for genuinely novel merchants, prompt-cached so it's near-free.

Schema: [`scripts/schema.sql`](scripts/schema.sql). Pipeline: [`ledger_one/pull.py`](ledger_one/pull.py).

## 5-minute quickstart

1. Install: `pip install -e ".[dev]"`
2. SimpleFIN Bridge account → claim token → `.env` (see [`references/simplefin_setup.md`](references/simplefin_setup.md)).
3. Neon Postgres → apply `scripts/schema.sql` (see [`references/neon_setup.md`](references/neon_setup.md)).
4. (Optional) Import Copilot history: `python scripts/import_copilot.py ~/copilot.csv --account-id <id> --before YYYY-MM-DD`.
5. `cp config/categories.yaml.example config/categories.yaml` and edit.
6. `python scripts/pull.py --days 90`.

Full walkthrough: [`SKILL.md`](SKILL.md).

## Extending

This is the data layer. Build your UI on top in a separate repo:

```python
from ledger_one.normalize import normalize_merchant
import psycopg
# ... read from transactions, write only to category_overrides
```

See [`references/extending.md`](references/extending.md) for the full pattern.

## Querying your data

Sample SQL: [`references/querying_data.md`](references/querying_data.md).

## License

MIT.
