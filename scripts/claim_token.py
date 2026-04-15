#!/usr/bin/env python3
"""Exchange a SimpleFIN setup token for a permanent access URL."""
import base64
import sys
import requests
from urllib.parse import urlparse


def _redact(url: str) -> str:
    p = urlparse(url)
    if p.username or p.password:
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return f"{p.scheme}://***:***@{host}{p.path}"
    return url


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/claim_token.py <base64-setup-token>")
        sys.exit(1)
    setup_token = sys.argv[1].strip()
    claim_url = base64.b64decode(setup_token).decode("ascii")
    print(f"Claiming: {_redact(claim_url)}")
    resp = requests.post(claim_url, timeout=60)
    resp.raise_for_status()
    access_url = resp.text.strip()
    print("\nSuccess. Paste this into your .env (keep it secret):")
    print(f"SIMPLEFIN_ACCESS_URL={access_url}")


if __name__ == "__main__":
    main()
