import base64
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CLAIM_TOKEN_PATH = Path(__file__).resolve().parent.parent / "scripts" / "claim_token.py"
CLAIM_TOKEN_SPEC = importlib.util.spec_from_file_location("claim_token", CLAIM_TOKEN_PATH)
claim_token = importlib.util.module_from_spec(CLAIM_TOKEN_SPEC)
assert CLAIM_TOKEN_SPEC and CLAIM_TOKEN_SPEC.loader
CLAIM_TOKEN_SPEC.loader.exec_module(claim_token)


def test_decode_claim_url_rejects_invalid_base64():
    with pytest.raises(ValueError, match="base64"):
        claim_token._decode_claim_url("not-base64")


def test_decode_claim_url_rejects_non_simplefin_host():
    token = base64.b64encode(b"https://example.com/claim").decode("ascii")
    with pytest.raises(ValueError, match="simplefin.org"):
        claim_token._decode_claim_url(token)


def test_upsert_env_var_updates_existing_value(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgres://db\nSIMPLEFIN_ACCESS_URL=old\n")

    claim_token._upsert_env_var(
        env_file,
        "SIMPLEFIN_ACCESS_URL",
        "https://u:p@bridge.simplefin.org/simplefin",
    )

    assert env_file.read_text().splitlines() == [
        "DATABASE_URL=postgres://db",
        "SIMPLEFIN_ACCESS_URL=https://u:p@bridge.simplefin.org/simplefin",
    ]


def test_main_writes_env_without_printing_secret(monkeypatch, tmp_path, capsys):
    claim_url = "https://beta-bridge.simplefin.org/claim"
    setup_token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")
    env_file = tmp_path / ".env"
    access_url = "https://user:pass@bridge.simplefin.org/simplefin"
    response = MagicMock()
    response.text = access_url
    response.raise_for_status.return_value = None

    monkeypatch.setattr(
        "sys.argv",
        ["claim_token.py", setup_token, "--env-file", str(env_file)],
    )

    with patch.object(claim_token.requests, "post", return_value=response) as post:
        assert claim_token.main() == 0

    out = capsys.readouterr().out
    assert "Claiming: https://beta-bridge.simplefin.org/claim" in out
    assert access_url not in out
    assert "Secret not printed." in out
    assert f"SIMPLEFIN_ACCESS_URL={access_url}" in env_file.read_text()
    post.assert_called_once_with(claim_url, timeout=60)
