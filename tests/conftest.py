import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse
import psycopg
import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "scripts" / "schema.sql"


def _configured_test_db_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or dotenv_values(ROOT / ".env.test").get("TEST_DATABASE_URL")


def _remote_test_db_fingerprint(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    db_name = (parsed.path or "").lstrip("/").lower()
    return f"{host}/{db_name}"


def _redact_db_url(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _test_db_url() -> str:
    url = _configured_test_db_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    db_name = (parsed.path or "").lstrip("/").lower()

    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("TEST_DATABASE_URL must be a postgres connection string")

    if host in {"localhost", "127.0.0.1", "::1"} and "test" in db_name:
        return url

    if os.environ.get("LEDGER_ONE_ALLOW_DESTRUCTIVE_TEST_DB") == _remote_test_db_fingerprint(url):
        return url

    pytest.skip(
        "Refusing destructive DB tests against a remote TEST_DATABASE_URL by default. "
        "Use a local database whose name contains 'test', or export "
        "LEDGER_ONE_ALLOW_DESTRUCTIVE_TEST_DB to the exact '<host>/<dbname>' target "
        "for a one-shell opt-in."
    )


@pytest.fixture
def db():
    url = _test_db_url()
    try:
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
    except psycopg.OperationalError:
        raise RuntimeError(
            f"Failed to connect to TEST_DATABASE_URL ({_redact_db_url(url)}). "
            "Check network access and database availability."
        ) from None
