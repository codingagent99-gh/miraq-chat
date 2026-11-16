"""
encrypt_shopify_tokens.py - encrypt Shopify tokens stored before encryption.

models/shopify_token.py now stores access_token and refresh_token encrypted
(tenant_crypto, key in TENANT_ENC_KEY) and still reads rows written earlier in
plain text. This rewrites those older rows so no token is left in plain text.

ORDER MATTERS
  1. Deploy the backend with the new models/shopify_token.py and restart it.
     Code from before that change would send an encrypted token to Shopify.
  2. Then run this script. It is safe to run again; encrypted rows are skipped.

Each row is updated only if it still holds the exact value read a moment
earlier. If the token manager refreshed that store in between (it writes
encrypted tokens now, and Shopify rotates refresh tokens, so the old one stops
working), the update matches nothing and the newer token is kept.

Usage:
    python encrypt_shopify_tokens.py --dry-run    # report only
    python encrypt_shopify_tokens.py
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def _is_encrypted(value) -> bool:
    # Same rule as models/shopify_token.is_encrypted (kept local so this script
    # doesn't import the Flask models).
    return bool(value) and value.startswith("gAAAAA")


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypt plain-text Shopify tokens in shopify_tokens.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    args = parser.parse_args()

    load_dotenv()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("encrypt_shopify_tokens: DATABASE_URL is not set", file=sys.stderr)
        return 2

    from tenant_crypto import decrypt_secret, encrypt_secret
    # Fail before touching anything if TENANT_ENC_KEY is missing or wrong.
    probe = "shpat_probe"
    if decrypt_secret(encrypt_secret(probe)) != probe:
        print("encrypt_shopify_tokens: TENANT_ENC_KEY round trip failed", file=sys.stderr)
        return 2

    engine = create_engine(dsn, pool_pre_ping=True)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT store_domain, access_token, refresh_token FROM shopify_tokens ORDER BY store_domain"
        )).all()

    done = already = changed = 0
    for shop, access, refresh in rows:
        plain_access = bool(access) and not _is_encrypted(access)
        plain_refresh = bool(refresh) and not _is_encrypted(refresh)
        if not plain_access and not plain_refresh:
            already += 1
            continue
        what = ", ".join(n for n, p in (("access token", plain_access), ("refresh token", plain_refresh)) if p)
        if args.dry_run:
            print(f"would encrypt {what} | shop={shop}")
            done += 1
            continue

        new_access = encrypt_secret(access) if plain_access else access
        new_refresh = encrypt_secret(refresh) if plain_refresh else refresh
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE shopify_tokens SET access_token = :new_access, refresh_token = :new_refresh "
                "WHERE store_domain = :shop AND access_token = :old_access "
                "AND refresh_token IS NOT DISTINCT FROM :old_refresh"
            ), {"new_access": new_access, "new_refresh": new_refresh, "shop": shop,
                "old_access": access, "old_refresh": refresh})
        if result.rowcount == 1:
            print(f"encrypted {what} | shop={shop}")
            done += 1
        else:
            print(f"skipped, token changed while running (kept the newer one) | shop={shop}")
            changed += 1

    engine.dispose()
    verb = "would encrypt" if args.dry_run else "encrypted"
    print(f"encrypt_shopify_tokens: {len(rows)} stores | {verb} {done} | already encrypted {already}"
          + ("" if args.dry_run else f" | changed meanwhile {changed}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
