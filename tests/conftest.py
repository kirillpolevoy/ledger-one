import os
from pathlib import Path
import psycopg
import pytest
from dotenv import load_dotenv

load_dotenv()

SCHEMA_FILE = Path(__file__).parent.parent / "scripts" / "schema.sql"


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
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
