import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from ledger_one.simplefin import fetch_accounts_and_transactions, _parse_access_url

FIX = Path(__file__).parent / "fixtures" / "simplefin_response.json"


def test_parses_url_credentials():
    base_url, auth = _parse_access_url("https://user:pass@bridge.simplefin.org/simplefin")
    assert base_url == "https://bridge.simplefin.org/simplefin"
    assert auth == ("user", "pass")


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
    assert len(accounts) == 1 and accounts[0]["id"] == "ACC-001"
    assert len(txns) == 1 and txns[0]["id"] == "TX-001"
    # both errors and errlist merged
    assert any("legacy" in e for e in errors)
    assert any("structured" in e for e in errors)
