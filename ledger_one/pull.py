import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ledger_one.normalize import normalize_merchant
from ledger_one.categorize import categorize_transactions
from ledger_one.config import load_categories
from ledger_one.db import upsert_accounts, upsert_transactions
from ledger_one.simplefin import fetch_accounts_and_transactions

log = logging.getLogger(__name__)

STALE_BALANCE_THRESHOLD = timedelta(hours=36)

# Reconciliation only trusts "absent from feed" for pendings comfortably inside
# the refetch window. SimpleFIN filters by transacted_at at DAY precision, so we
# hold back this many days from the window edge to avoid a day-boundary mismatch
# false-dropping a still-live pending right at the cutoff.
RECONCILE_WINDOW_BUFFER_DAYS = 2


def _warn_on_stale_balances(accounts, now=None):
    now = now or datetime.now(timezone.utc)
    stale = []
    for acct in accounts:
        bd = acct.get("balance_date")
        if not bd:
            continue
        age = now - datetime.fromisoformat(bd)
        if age > STALE_BALANCE_THRESHOLD:
            stale.append(acct["name"])
            log.warning(
                "Stale balance for %s (%s): %.1fh old — SimpleFIN may be serving cached data",
                acct["name"], acct.get("institution") or "?", age.total_seconds() / 3600,
            )
    return stale


def _classify_txns(raw_txns, existing):
    """Bucket fetched txns by how they should be handled downstream.

    `existing` is a dict[str, bool] mapping transaction id -> current pending
    flag in the DB (only ids we already have).

    A txn is a transition iff the DB row is pending AND the payload now carries
    a real `posted` timestamp (has_real_posted=True). We use `has_real_posted`
    rather than `not pending` because SimpleFIN sometimes flips `posted` on
    *before* clearing `pending` (the flip moment). Missing that case would leave
    the row stuck at `pending=true` with a stale posted_at.

    Returns (truly_new, transitions, already_seen):
      - truly_new:    id not in DB; needs categorization + INSERT
      - transitions:  DB row is pending AND payload has a real posted timestamp;
                      UPSERT updates state, existing category is preserved
      - already_seen: in DB already, nothing to do
    """
    truly_new, transitions, already_seen = [], [], []
    for tx in raw_txns:
        if tx["id"] not in existing:
            truly_new.append(tx)
        elif existing[tx["id"]] and tx.get("has_real_posted"):
            transitions.append(tx)
        else:
            already_seen.append(tx)
    return truly_new, transitions, already_seen


def _find_duplicate_pending_suspects(db, truly_new):
    """Detect possible id-rotation: a new posted txn that matches a recent pending
    by secondary key (account_id, amount, merchant_pattern) within 3 days.

    If this count is ever non-zero, the "ids stay stable through pending→posted"
    assumption is violated for at least one txn — inspect and consider a
    secondary-key dedup fallback.
    """
    candidates = [
        (t["account_id"], t["amount"], t["merchant_pattern"])
        for t in truly_new if not t.get("pending")
    ]
    # Single batch query: for each candidate, does a pending row exist within 3d
    # with matching (account_id, amount, merchant_pattern)? UNNEST of empty
    # arrays returns zero rows, so we don't need a `if not candidates` guard.
    rows = db.execute(
        """
        SELECT DISTINCT p.id
        FROM transactions p
        JOIN (SELECT * FROM UNNEST(%s::text[], %s::numeric[], %s::text[])
              AS c(account_id, amount, merchant_pattern)) c
          ON p.account_id = c.account_id
         AND p.amount = c.amount
         AND p.merchant_pattern = c.merchant_pattern
        WHERE p.pending = true
          AND p.posted_at > now() - interval '3 days'
        """,
        (
            [c[0] for c in candidates],
            [c[1] for c in candidates],
            [c[2] for c in candidates],
        ),
    ).fetchall()
    if rows:
        log.warning(
            "Possible id-rotation: %d pending rows match new posted rows by "
            "(account_id, amount, merchant_pattern). Inspect: %s",
            len(rows), [r[0] for r in rows],
        )
    return len(rows)


def _reconcile_absent_pendings(db, feed_ids, window_start, *, dry_run):
    """Drop pending rows SimpleFIN no longer reports within the refetch window.

    A pending that's gone from the feed has either settled under a new id (the
    real posted row is already in the ledger, inserted as a truly-new row) or
    had its hold released — either way the stranded pending is dead. This is the
    deterministic equivalent of Plaid's `removed` event: SimpleFIN gives us no
    pending→posted link, so "present last pull, absent this pull" is the signal.

    Only rows with posted_at >= window_start are eligible: outside that window
    SimpleFIN wasn't queried for them, so absence is uninformative. A false drop
    from a transient feed gap is self-healing — the next pull sees the id is
    gone from the DB and re-inserts it as new.

    Returns the dropped (id, description) rows for the audit log. In dry_run it
    SELECTs the would-drop set without deleting.
    """
    feed_ids = list(feed_ids)
    with db.cursor() as cur:
        if dry_run:
            cur.execute(
                """
                SELECT id, description, amount, posted_at FROM transactions
                WHERE pending = true
                  AND posted_at >= %s
                  AND NOT (id = ANY(%s))
                """,
                (window_start, feed_ids),
            )
        else:
            cur.execute(
                """
                DELETE FROM transactions
                WHERE pending = true
                  AND posted_at >= %s
                  AND NOT (id = ANY(%s))
                RETURNING id, description, amount, posted_at
                """,
                (window_start, feed_ids),
            )
        return cur.fetchall()


def run_pull(
    *,
    db,
    access_url: str,
    days: int,
    categories_file: Path,
    anthropic_client,
    model: str,
    simplefin_fetcher=fetch_accounts_and_transactions,
    dry_run: bool = False,
) -> dict:
    accounts, raw_txns, errors = simplefin_fetcher(access_url, days)
    for err in errors:
        log.warning("SimpleFIN error: %s", err)
    stale_accounts = _warn_on_stale_balances(accounts)

    for tx in raw_txns:
        tx["merchant_pattern"] = normalize_merchant(tx["description"])

    # Concurrent pulls are safe without explicit locking:
    #  - `upsert_transactions` has a WHERE transactions.pending=true guard that
    #    blocks a second writer from mutating a row already flipped to posted.
    #  - The learn trigger's `IS DISTINCT FROM` check suppresses merchant_categories
    #    writes when the category didn't actually change (as on a transition).
    # So we intentionally don't hold a row lock across the AI-categorization call
    # that happens between SELECT and UPSERT.
    existing: dict[str, bool] = {}
    if raw_txns:
        rows = db.execute(
            "SELECT id, pending FROM transactions WHERE id = ANY(%s)",
            ([tx["id"] for tx in raw_txns],),
        ).fetchall()
        existing = {id_: pending for id_, pending in rows}

    truly_new, transitions, _already_seen = _classify_txns(raw_txns, existing)

    # Only truly-new txns get categorized. Transitions preserve the category
    # that was assigned when the row was first inserted as pending.
    categories = load_categories(categories_file)
    results = categorize_transactions(
        db, truly_new,
        categories=categories,
        anthropic_client=anthropic_client,
        model=model,
    )
    for tx in truly_new:
        cat, source = results[tx["id"]]
        tx["category"] = cat
        tx["source"] = source

    # Transitions don't carry category/source — db.py reads them from EXCLUDED
    # but the UPDATE SET list omits category/categorization_source/categorized_at
    # so the stored values are preserved. We still need placeholder keys for the
    # tuple-building step in upsert_transactions.
    # Also force `pending = False`: the transition means the txn has really
    # posted, even if SimpleFIN's payload still has `pending: true` during the
    # flip moment. has_real_posted is authoritative here.
    for tx in transitions:
        tx.setdefault("category", None)
        tx.setdefault("source", None)
        tx["pending"] = False

    to_write = truly_new + transitions

    # Reconciliation: drop pendings SimpleFIN stopped reporting within the
    # refetch window. Skipped on an empty feed (likely an outage) or when
    # SimpleFIN returned errors (a partial feed would make a live account's
    # pendings look falsely absent) — never delete on incomplete data.
    feed_ids = [tx["id"] for tx in raw_txns]
    reconcile_window_start = datetime.now(timezone.utc) - timedelta(
        days=max(days - RECONCILE_WINDOW_BUFFER_DAYS, 0)
    )
    reconcile_enabled = bool(feed_ids) and not errors

    if dry_run:
        log.info("Dry run — skipping database writes.")
        for tx in to_write:
            tag = "PENDING" if tx.get("pending") else "POSTED "
            log.info(
                "  [%s] %s | %s | %s | %s",
                tag, tx["posted_at"][:10], tx["description"][:40], tx["amount"],
                tx.get("category") or "(preserved)",
            )
        inserted, updated = 0, 0
        duplicate_pending_suspects = _find_duplicate_pending_suspects(db, truly_new)
        dropped = (
            _reconcile_absent_pendings(db, feed_ids, reconcile_window_start, dry_run=True)
            if reconcile_enabled else []
        )
    else:
        with db.transaction():
            upsert_accounts(db, accounts)
            inserted, updated = upsert_transactions(db, to_write)
            duplicate_pending_suspects = _find_duplicate_pending_suspects(db, truly_new)
            dropped = (
                _reconcile_absent_pendings(db, feed_ids, reconcile_window_start, dry_run=False)
                if reconcile_enabled else []
            )

    if not reconcile_enabled:
        log.info(
            "Pending reconciliation skipped (%s).",
            "empty feed" if not feed_ids else "SimpleFIN errors present",
        )
    elif dropped:
        log.info(
            "%s %d stale pending row(s) absent from feed:",
            "Would drop" if dry_run else "Dropped", len(dropped),
        )
        for tx_id, desc, amount, posted_at in dropped:
            log.info(
                "  - %s | %s | %s | %s",
                str(posted_at)[:10], (desc or "")[:40], amount, tx_id,
            )

    stats = {
        "accounts": len(accounts),
        "transactions_fetched": len(raw_txns),
        "pending_inserts": sum(1 for t in truly_new if t.get("pending")),
        "posted_inserts": sum(1 for t in truly_new if not t.get("pending")),
        "pending_to_posted_transitions": len(transitions),
        "duplicate_pending_suspects": duplicate_pending_suspects,
        "pendings_dropped": len(dropped),
        "override_matches": sum(1 for _, s in results.values() if s == "override"),
        "learned_matches": sum(1 for _, s in results.values() if s == "learned"),
        "ai_calls": sum(1 for _, s in results.values() if s == "ai"),
        "upserted": inserted + updated,
        "dry_run": dry_run,
        "errors": errors,
        "stale_accounts": stale_accounts,
    }
    log.info("pull stats: %s", stats)
    return stats
