"""
purge_chat_history.py - delete chat data older than the retention period.

The privacy policy (Section 6) says chat history is kept for up to one month.
purge_logs.py covers the log folders; this covers the database side. For every
active tenant database it deletes:

  messages                      created_at   older than the cutoff
  conversations                 updated_at   older than the cutoff (their messages first)
  customer_visits              last_seen_at older than the cutoff
  shopify_order_confirmations   created_at   older than the cutoff

A conversation that is still being used has a recent updated_at and is kept,
but its individual messages older than the cutoff are still removed.

Run it daily from cron, next to purge_logs.py, e.g.:

    45 2 * * * /home/apps/varchaswi/miraq-chat/.venv/bin/python \
        /home/apps/varchaswi/<project>/purge_chat_history.py >> \
        /home/apps/varchaswi/<project>/logs/purge.log 2>&1

Archived tenants are skipped on purpose: their whole database is dropped by
refresh_scheduler after TENANT_ARCHIVE_GRACE_DAYS.

Usage:
    python purge_chat_history.py                 # keep 30 days
    python purge_chat_history.py --days 14
    python purge_chat_history.py --dry-run       # count what would be deleted
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

DEFAULT_KEEP_DAYS = 30

# (table, timestamp column) — deleted in this order. messages must go before
# conversations because of the foreign key.
_TARGETS = [
    ("messages", "created_at"),
    ("conversations", "updated_at"),
    ("customer_visits", "last_seen_at"),
    ("shopify_order_confirmations", "created_at"),
]


def _dsn_for(base_dsn: str, db_name: str) -> str:
    # Same rule as DBEngineRegistry._dsn_for: swap only the database segment.
    head, _sep, _old = base_dsn.rpartition("/")
    return f"{head}/{db_name}"


def purge_tenant(dsn: str, cutoff: datetime, dry_run: bool) -> dict:
    """Delete (or count) old rows in one tenant database. Atomic per tenant."""
    engine = create_engine(dsn, pool_pre_ping=True)
    counts: dict = {}
    try:
        with engine.begin() as conn:  # one transaction: all tables or none
            for table, column in _TARGETS:
                # Not every tenant database has every table (e.g. Woo tenants
                # have no shopify_order_confirmations).
                if not conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar():
                    continue
                if dry_run:
                    n = conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE {column} < :c"),
                        {"c": cutoff},
                    ).scalar()
                else:
                    n = conn.execute(
                        text(f"DELETE FROM {table} WHERE {column} < :c"),
                        {"c": cutoff},
                    ).rowcount
                counts[table] = n
    finally:
        engine.dispose()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete chat data older than N days.")
    parser.add_argument("--days", type=int, default=DEFAULT_KEEP_DAYS,
                        help=f"days of chat data to keep (default {DEFAULT_KEEP_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="count the rows that would be deleted, delete nothing")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")

    load_dotenv()
    base_dsn = os.getenv("DATABASE_URL")
    if not base_dsn:
        print("purge_chat_history: DATABASE_URL is not set", file=sys.stderr)
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    verb = "would delete" if args.dry_run else "deleted"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    main_engine = create_engine(base_dsn, pool_pre_ping=True)
    try:
        with main_engine.connect() as conn:
            tenants = conn.execute(
                text("SELECT tenant_id, db_name FROM tenants WHERE status = 'active'")
            ).fetchall()
    finally:
        main_engine.dispose()

    failed = 0
    for tenant_id, db_name in tenants:
        try:
            counts = purge_tenant(_dsn_for(base_dsn, db_name), cutoff, args.dry_run)
            print(f"[{stamp}] purge_chat_history: {verb} {counts} "
                  f"older than {args.days} days | tenant={tenant_id}")
        except Exception as e:
            # One broken tenant must not stop the others; exit non-zero so cron
            # mail / monitoring notices that retention was not enforced.
            failed += 1
            print(f"[{stamp}] purge_chat_history: FAILED | tenant={tenant_id} | {e}",
                  file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())