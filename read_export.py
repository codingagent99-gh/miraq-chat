#!/usr/bin/env python3
"""
read_export.py - open a Shopify customers/data_request export.

With ENCRYPT_CUSTOMER_DATA=1, shopify_compliance.py writes the export as
compliance_exports/<shop>/data_request_<id>.json.enc. The merchant needs the
plain JSON, so before sending it:

    python read_export.py compliance_exports/<shop>/data_request_<id>.json.enc
        # prints a summary
    python read_export.py <file>.json.enc --out /tmp/data_request_<id>.json
        # writes the plain JSON (owner-only) to send

Send the plain file to the merchant through a secure channel, then delete it:
it is unencrypted shopper data. Plain .json exports (written while the switch
was off) open the same way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import customer_crypto as cc


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if raw.startswith(cc.ENC_PREFIX):
        if not cc.key_configured():
            raise SystemExit("CUSTOMER_DATA_KEY is not set (.env)")
        plain = cc.decrypt_text(raw)
        if plain is raw:
            raise SystemExit("could not decrypt: wrong CUSTOMER_DATA_KEY?")
        raw = plain
    return json.loads(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description="Open a data-request export.")
    ap.add_argument("file")
    ap.add_argument("--out", help="write the plain JSON here (owner-only file)")
    args = ap.parse_args()

    data = load(args.file)
    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"wrote {args.out} - plain shopper data: send it securely, then delete it")
        return 0

    convs = data.get("conversations") or []
    msgs = sum(len(c.get("messages") or []) for c in convs)
    print(f"shop            {data.get('shop_domain')}")
    print(f"request         {data.get('data_request_id')}")
    print(f"customer        {data.get('customer_id')}")
    print(f"generated       {data.get('generated_at')}")
    print(f"conversations   {len(convs)} ({msgs} messages)")
    print(f"order confirms  {len(data.get('order_confirmations') or [])}")
    print(f"channel links   {len(data.get('channel_links') or [])}")
    print("use --out <file> to write the plain JSON for the merchant")
    return 0


if __name__ == "__main__":
    sys.exit(main())
