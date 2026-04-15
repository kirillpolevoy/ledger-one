# SimpleFIN Bridge setup

1. Go to [https://beta-bridge.simplefin.org](https://beta-bridge.simplefin.org) and create an account. Cost: ~$15/year.
2. In the SimpleFIN dashboard, click "Connect" to link each bank. This uses the bank's OAuth flow; SimpleFIN stores read-only credentials.
3. Wait for banks to sync (a few minutes to an hour).
4. Click "Manage" → "New Setup Token" to generate a **base64 setup token**. This token is single-use and short-lived.
5. Claim the token immediately with:
   ```
   python scripts/claim_token.py <base64-token-here>
   ```
   This prints a permanent `SIMPLEFIN_ACCESS_URL` — paste it into `.env`.

**Note:** the access URL contains HTTP Basic auth credentials. Treat it as a secret. `ledger-one` parses the credentials out before making requests and never logs the raw URL.
