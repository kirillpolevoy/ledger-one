import os
from pathlib import Path
from urllib.parse import urlparse
import psycopg
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env.test")

SCHEMA_FILE = ROOT / "scripts" / "schema.sql"


def _test_db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    db_name = (parsed.path or "").lstrip("/").lower()

    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("TEST_DATABASE_URL must be a postgres connection string")

    if host in {"localhost", "127.0.0.1", "::1"} and "test" in db_name:
        return url

    if os.environ.get("LEDGER_ONE_ALLOW_DESTRUCTIVE_TEST_DB") == "1":
        return url

    raise RuntimeError(
        "Refusing to run destructive DB tests against TEST_DATABASE_URL. "
        "Use a local database whose name contains 'test', or set "
        "LEDGER_ONE_ALLOW_DESTRUCTIVE_TEST_DB=1 for an isolated remote test database."
    )


@pytest.fixture
def db():
    url = _test_db_url()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("""
            DROP TABLE IF EXISTS transactions CASCADE;
            DROP TABLE IF EXISTS accounts CASCADE;
            DROP TABLE IF EXISTS merchant_categories CASCADE;
            DROP TABLE IF EXISTS category_overrides CASCADE;
            DROP FUNCTION IF EXISTS ledger_one_learn_on_update() CASCADE;
        """)
        conn.execute(SCHEMA_FILE.read_text())
        yield conn
