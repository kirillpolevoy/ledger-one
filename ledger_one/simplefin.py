from datetime import datetime, timedelta, timezone
from urllib.parse import ParseResult, urlparse, urlunparse
import requests

_SIMPLEFIN_HOST_SUFFIX = ".simplefin.org"


def _validate_simplefin_url(url: str, *, field_name: str) -> ParseResult:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme != "https":
        raise ValueError(f"{field_name} must use HTTPS")
    if not host or (host != "simplefin.org" and not host.endswith(_SIMPLEFIN_HOST_SUFFIX)):
        raise ValueError(f"{field_name} must point to a simplefin.org host")
    return parsed


def _parse_access_url(access_url: str) -> tuple[str, tuple[str, str] | None]:
    """Split userinfo out of the URL so credentials don't end up in logs.

    Also validates the URL is HTTPS and points to a known SimpleFIN host.
    """
    parsed = _validate_simplefin_url(access_url, field_name="SimpleFIN access URL")
    auth: tuple[str, str] | None = None
    if parsed.username or parsed.password:
        auth = (parsed.username or "", parsed.password or "")
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed), auth


def fetch_accounts_and_transactions(access_url: str, days: int):
    base_url, auth = _parse_access_url(access_url)
    start_dt = datetime.now(timezone.utc) - timedelta(days=days)
    params = {"start-date": int(start_dt.timestamp())}
    resp = requests.get(f"{base_url}/accounts", params=params, auth=auth, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    accounts = []
    txns = []
    for a in payload.get("accounts", []):
        accounts.append({
            "id": a["id"],
            "name": a.get("name", ""),
            "institution": (a.get("org") or {}).get("name"),
            "currency": a.get("currency", "USD"),
            "balance": a.get("balance"),
            "balance_date": (
                datetime.fromtimestamp(a["balance-date"], tz=timezone.utc).isoformat()
                if a.get("balance-date") else None
            ),
        })
        for t in a.get("transactions", []):
            txns.append({
                "id": t["id"],
                "account_id": a["id"],
                "amount": t["amount"],
                "description": t.get("description", ""),
                "posted_at": datetime.fromtimestamp(t["posted"], tz=timezone.utc).isoformat(),
                "raw_payload": t,
            })

    errors: list[str] = []
    for e in payload.get("errors") or []:
        errors.append(str(e))
    for e in payload.get("errlist") or []:
        if isinstance(e, dict):
            msg = e.get("msg") or ""
            acct = e.get("account_id") or ""
            errors.append(f"[{acct}] {msg}" if acct else msg)
        else:
            errors.append(str(e))
    return accounts, txns, errors
