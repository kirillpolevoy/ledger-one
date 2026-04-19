import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from ledger_one.simplefin import (
    _parse_access_url,
    _validate_simplefin_url,
    fetch_accounts_and_transactions,
)

FIX = Path(__file__).parent / "fixtures" / "simplefin_response.json"


def test_parses_url_credentials():
    base_url, auth = _parse_access_url("https://user:pass@bridge.simplefin.org/simplefin")
    assert base_url == "https://bridge.simplefin.org/simplefin"
    assert auth == ("user", "pass")


def test_rejects_non_https_simplefin_url():
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_simplefin_url("http://bridge.simplefin.org/simplefin", field_name="SimpleFIN URL")


def test_rejects_non_simplefin_host():
    with pytest.raises(ValueError, match="simplefin.org"):
        _parse_access_url("https://user:pass@example.com/simplefin")


def test_fetches_accounts_and_merges_errors():
    payload = json.loads(FIX.read_text())
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    with patch("ledger_one.simplefin.requests.get", return_value=mock_resp) as g:
        accounts, txns, errors = fetch_accounts_and_transactions(
            "https://u:p@bridge.simplefin.org/simplefin", days=7
        )
    call = g.call_args
    # URL passed to requests must not contain credentials
    assert "u:p@" not in call.args[0]
    # credentials passed separately as auth tuple
    assert call.kwargs["auth"] == ("u", "p")
    # pending=1 is always passed so SimpleFIN surfaces pending txns
    assert call.kwargs["params"]["pending"] == 1
    assert len(accounts) == 1 and accounts[0]["id"] == "ACC-001"
    assert len(txns) == 1 and txns[0]["id"] == "TX-001"
    assert txns[0]["pending"] is False  # fixture has no pending flag
    assert txns[0]["has_real_posted"] is True  # fixture has `posted` set
    # both errors and errlist merged
    assert any("legacy" in e for e in errors)
    assert any("structured" in e for e in errors)


def _payload_with_txn(**tx_overrides):
    base = {
        "accounts": [{
            "id": "ACC-001", "name": "Chase", "currency": "USD",
            "balance": "0", "balance-date": 1744704000,
            "org": {"name": "Chase"},
            "transactions": [dict({
                "id": "TX-999", "amount": "-10.00",
                "description": "AMAZON.COM",
            }, **tx_overrides)],
        }],
    }
    return base


def _fetch(payload):
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None
    with patch("ledger_one.simplefin.requests.get", return_value=mock_resp):
        return fetch_accounts_and_transactions(
            "https://u:p@bridge.simplefin.org/simplefin", days=7
        )


def test_parse_pending_uses_transacted_at_and_flags_pending():
    """Pending txns from SimpleFIN have `transacted_at` but no `posted`."""
    accounts, txns, errors = _fetch(_payload_with_txn(
        pending=True, transacted_at=1744617600,
    ))
    assert errors == []
    assert txns[0]["pending"] is True
    assert txns[0]["has_real_posted"] is False
    assert txns[0]["posted_at"].startswith("2025-04-14")  # transacted_at epoch


def test_parse_prefers_posted_over_transacted_at():
    """When both are present (the flip moment), `posted` wins for posted_at
    AND has_real_posted is True so downstream detects the transition."""
    accounts, txns, errors = _fetch(_payload_with_txn(
        pending=True, posted=1744704000, transacted_at=1744617600,
    ))
    assert errors == []
    assert txns[0]["pending"] is True  # payload still says pending
    assert txns[0]["has_real_posted"] is True  # but has real posted timestamp
    assert txns[0]["posted_at"].startswith("2025-04-15")  # posted, not transacted_at


def test_parse_skips_and_reports_when_neither_date_present():
    """Malformed txn is skipped with a warning in errors — does NOT raise."""
    payload = _payload_with_txn()
    # Strip posted from the default fixture-like txn (it wasn't set anyway)
    # by ensuring there's also no transacted_at. Both are absent.
    payload["accounts"][0]["transactions"][0].pop("posted", None)
    payload["accounts"][0]["transactions"][0].pop("transacted_at", None)
    accounts, txns, errors = _fetch(payload)
    assert txns == []
    assert any("TX-999" in e and "posted" in e for e in errors)
