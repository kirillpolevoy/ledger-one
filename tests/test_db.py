from ledger_one.db import upsert_accounts, insert_transactions


def _account(**overrides):
    base = {"id": "a1", "name": "Chase", "institution": "Chase Bank",
            "currency": "USD", "balance": "1000.00",
            "balance_date": "2026-04-15T00:00:00+00:00"}
    base.update(overrides)
    return base


def test_upsert_account_insert_then_update(db):
    upsert_accounts(db, [_account(balance="1000.00")])
    assert db.execute("SELECT last_balance FROM accounts WHERE id='a1'").fetchone() == (1000,)
    upsert_accounts(db, [_account(balance="1500.00")])
    assert db.execute("SELECT last_balance FROM accounts WHERE id='a1'").fetchone() == (1500,)


def test_insert_transactions_idempotent(db):
    upsert_accounts(db, [_account()])
    txns = [{
        "id": "tx1", "account_id": "a1", "amount": "-5.00",
        "description": "STARBUCKS", "merchant_pattern": "starbucks",
        "category": "Coffee", "source": "ai",
        "posted_at": "2026-04-14T12:00:00+00:00", "raw_payload": {},
    }]
    assert insert_transactions(db, txns) == 1
    assert insert_transactions(db, txns) == 0  # idempotent


def test_insert_transactions_batch(db):
    upsert_accounts(db, [_account()])
    txns = [{
        "id": f"tx{i}", "account_id": "a1", "amount": "-1.00",
        "description": f"MERCHANT {i}", "merchant_pattern": f"merchant {i}",
        "category": "Shopping", "source": "ai",
        "posted_at": "2026-04-14T12:00:00+00:00", "raw_payload": {},
    } for i in range(100)]
    assert insert_transactions(db, txns) == 100
