#!/usr/bin/env python3
"""
inspect_chat.py - read stored chats in plain text, for debugging.

With ENCRYPT_CUSTOMER_DATA=1, psql shows message text, context and customer
IDs as enc1:/cid1: tokens. This prints them decrypted. Run it on the server,
from the project folder (it needs the project's .env and CUSTOMER_DATA_KEY).

    python inspect_chat.py <session_id>                        # one chat, any store
    python inspect_chat.py --shop tiles.myshopify.com --last 5 # a store's latest chats
    python inspect_chat.py --shop tiles.myshopify.com --customer 8123456789012
    python inspect_chat.py <session_id> --meta                 # also print metadata JSON
    python inspect_chat.py <session_id> --context              # also print context_data

Read-only. The output is plain shopper data: don't paste it into tickets or
chat tools; quote only what you need.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from sqlalchemy import select

import customer_data_db as cdb
from models.db_models import Conversation, Message

CONV = Conversation.__table__
MSG = Message.__table__


def _short(meta) -> str:
    if not isinstance(meta, dict):
        return ""
    bits = []
    products = meta.get("products")
    if isinstance(products, list) and products:
        names = [str(p.get("name")) for p in products if isinstance(p, dict) and p.get("name")]
        bits.append(f"{len(products)} products: " + ", ".join(names[:3]) + ("…" if len(names) > 3 else ""))
    orders = meta.get("orders")
    if isinstance(orders, list) and orders:
        bits.append("orders: " + ", ".join(str(o.get("order_number")) for o in orders if isinstance(o, dict)))
    i18n = meta.get("i18n")
    if isinstance(i18n, dict) and i18n.get("lang"):
        bits.append(f"lang {i18n.get('lang')}")
    return " | ".join(bits)


def print_conversation(conn, conv_row, show_meta: bool, show_context: bool) -> None:
    c = conv_row._mapping
    print("=" * 78)
    print(f"session   {c['id']}")
    print(f"customer  {c['customer_id'] or 'guest'}")
    print(f"started   {c['created_at']:%Y-%m-%d %H:%M:%S}   last activity {c['updated_at']:%Y-%m-%d %H:%M:%S}"
          if c["created_at"] and c["updated_at"] else "")
    print(f"flow      {c['flow_state']}")
    if show_context:
        print("context   " + json.dumps(c["context_data"], ensure_ascii=False, indent=2, default=str))
    print("-" * 78)
    rows = conn.execute(select(MSG).where(MSG.c.conversation_id == c["id"])
                        .order_by(MSG.c.created_at, MSG.c.id))
    for m in rows:
        m = m._mapping
        when = f"{m['created_at']:%m-%d %H:%M:%S}" if m["created_at"] else "?"
        intent = f" ({m['intent']})" if m["intent"] else ""
        print(f"[{when}] #{m['id']} {m['role'].upper()}{intent}")
        for line in str(m["content"] or "").splitlines() or [""]:
            print(f"    {line}")
        extra = _short(m["metadata_json"])
        if extra:
            print(f"    · {extra}")
        if show_meta and m["metadata_json"]:
            print("    meta " + json.dumps(m["metadata_json"], ensure_ascii=False, default=str))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Print stored chats decrypted.")
    ap.add_argument("session_id", nargs="?", help="conversation / session id (UUID)")
    ap.add_argument("--shop", help="store: shopify domain, site domain or db name")
    ap.add_argument("--customer", help="Shopify (or WordPress) customer ID")
    ap.add_argument("--last", type=int, default=5, help="how many recent chats (default 5)")
    ap.add_argument("--meta", action="store_true", help="print each message's metadata JSON")
    ap.add_argument("--context", action="store_true", help="print the conversation context")
    args = ap.parse_args()

    if not args.session_id and not args.shop:
        ap.error("give a session id, or --shop")
    sid = None
    if args.session_id:
        try:
            sid = uuid.UUID(args.session_id)
        except ValueError:
            ap.error("session id must be a UUID")

    tenants = cdb.list_tenants(args.shop, include_inactive=True)
    if not tenants:
        print("no matching store", file=sys.stderr)
        return 1

    found = 0
    for t in tenants:
        eng = cdb.engine_for(t.db_name)
        try:
            with eng.connect() as conn:
                if not cdb.has_table(conn, "conversations"):
                    continue
                q = select(CONV)
                if sid:
                    q = q.where(CONV.c.id == sid)
                else:
                    if args.customer:
                        q = q.where(Conversation.customer_id == args.customer)  # matches both forms
                    q = q.order_by(CONV.c.updated_at.desc()).limit(max(1, args.last))
                convs = conn.execute(q).fetchall()
                if convs:
                    print(f"## store {t.shop} ({t.db_name}, {t.status})")
                for conv in convs:
                    print_conversation(conn, conv, args.meta, args.context)
                    found += 1
        finally:
            eng.dispose()
        if sid and found:
            break

    if not found:
        print("no chats found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
