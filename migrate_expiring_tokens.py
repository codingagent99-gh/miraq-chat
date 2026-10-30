"""
migrate_expiring_tokens.py - one-off: convert legacy NON-expiring Shopify
offline tokens into expiring ones (Shopify deadline: 1 Jan 2027).

New installs already get an expiring token (routes/shopify_oauth.py sends
expiring=1). A store installed before that still holds the old non-expiring
token in shopify_tokens, with no refresh_token. This script finds those rows
and swaps each token through Shopify's token-exchange grant:

    POST https://<shop>/admin/oauth/access_token
        grant_type           urn:ietf:params:oauth:grant-type:token-exchange
        subject_token        <the existing non-expiring offline token>
        subject_token_type   urn:shopify:params:oauth:token-type:offline-access-token
        requested_token_type urn:shopify:params:oauth:token-type:offline-access-token
        expiring             1

No merchant action is needed. After this, ShopifyTokenManager takes over with
the normal refresh_token grant.

IMPORTANT - Shopify invalidates the old token the moment the exchange succeeds,
so the new pair is written in the SAME transaction (and under the same
per-shop advisory lock ShopifyTokenManager uses, so a refresh cannot race it).
If the process dies after Shopify answered but before the commit, just run the
script again: Shopify returns the same token pair for a repeat of the same
request for 7 days.

Rows whose token is already expiring (e.g. issued by client_credentials) are
rejected by Shopify with invalid_subject_token; they are reported and left
untouched.

Usage (from the project dir, same .env as the server):
    python migrate_expiring_tokens.py --dry-run          # list candidates only
    python migrate_expiring_tokens.py                    # migrate all candidates
    python migrate_expiring_tokens.py --shop x.myshopify.com

After a real run, restart the app (pm2 restart ...) so workers drop the old
token they have cached in memory.
"""

import argparse
import sys
import zlib
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

_OFFLINE = "urn:shopify:params:oauth:token-type:offline-access-token"
_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"


def exchange_to_expiring(shop: str, token: str, client_id: str, client_secret: str):
    """Returns (payload, None) on success, else (None, reason).

    reason is "invalid_subject_token" when Shopify says the token is not an
    active non-expiring offline token (already expiring, revoked, or another
    app's); anything else is a transport/HTTP error text.
    """
    try:
        resp = requests.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "grant_type":           _EXCHANGE,
                "client_id":            client_id,
                "client_secret":        client_secret,
                "subject_token":        token,
                "subject_token_type":   _OFFLINE,
                "requested_token_type": _OFFLINE,
                "expiring":             "1",
            },
            timeout=20,
        )
    except Exception as e:
        return None, f"could not reach {shop}: {type(e).__name__}: {e}"

    if resp.status_code == 400 and "invalid_subject_token" in resp.text:
        return None, "invalid_subject_token"
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code} {resp.text[:300]}"
    try:
        payload = resp.json()
    except ValueError:
        return None, f"non-JSON answer: {resp.text[:200]!r}"
    if not all(payload.get(k) for k in ("access_token", "refresh_token", "expires_in")):
        # Never overwrite a row with a response that lacks the refresh token.
        return None, f"response is not an expiring token (keys={sorted(payload)})"
    return payload, None


def _build_app():
    import os
    from flask import Flask
    from models import db

    uri = os.getenv("DATABASE_URL")
    if not uri:
        print("migrate_expiring_tokens: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(2)
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate non-expiring Shopify offline tokens.")
    parser.add_argument("--dry-run", action="store_true", help="list candidate shops, change nothing")
    parser.add_argument("--shop", help="only this <store>.myshopify.com")
    args = parser.parse_args()

    load_dotenv()
    app = _build_app()

    from sqlalchemy import text
    from models import db
    from models.shopify_token import ShopifyToken
    from shopify_apps import app_for_shop

    failed = 0
    with app.app_context():
        q = ShopifyToken.query.filter(ShopifyToken.refresh_token.is_(None))
        if args.shop:
            q = q.filter(ShopifyToken.store_domain == args.shop.strip().lower())
        shops = [r.store_domain for r in q.all()]
        print(f"{len(shops)} token row(s) without a refresh token")

        for shop in shops:
            if args.dry_run:
                print(f"  would try: {shop}")
                continue
            try:
                # Same lock key as ShopifyTokenManager._do_refresh.
                db.session.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": zlib.crc32(shop.encode("utf-8"))},
                )
                row = db.session.get(ShopifyToken, shop)
                db.session.refresh(row)  # re-read under the lock
                if row is None or row.refresh_token:
                    db.session.rollback()
                    print(f"  skip {shop}: already migrated")
                    continue

                creds = app_for_shop(shop)
                if not creds.configured:
                    db.session.rollback()
                    print(f"  FAIL {shop}: no app credentials configured", file=sys.stderr)
                    failed += 1
                    continue

                payload, err = exchange_to_expiring(
                    shop, row.access_token, creds.client_id, creds.client_secret
                )
                if err == "invalid_subject_token":
                    db.session.rollback()
                    print(f"  skip {shop}: token is not a non-expiring offline token "
                          f"(already expiring, revoked, or not this app's)")
                    continue
                if err:
                    db.session.rollback()
                    print(f"  FAIL {shop}: {err}", file=sys.stderr)
                    failed += 1
                    continue

                now = datetime.now(timezone.utc)
                row.access_token = payload["access_token"]
                row.scope = payload.get("scope") or row.scope
                row.fetched_at = now
                row.expires_at = now + timedelta(seconds=int(payload["expires_in"]))
                row.refresh_token = payload["refresh_token"]
                rte = payload.get("refresh_token_expires_in")
                row.refresh_token_expires_at = now + timedelta(seconds=int(rte)) if rte else None
                row.refresh_count = (row.refresh_count or 0) + 1
                row.last_error = None
                db.session.commit()  # persists the new pair, releases the lock
                print(f"  OK   {shop}: now expiring, access token valid "
                      f"{int(payload['expires_in'])}s, refresh token stored")
            except Exception as e:
                db.session.rollback()
                print(f"  FAIL {shop}: {type(e).__name__}: {e}", file=sys.stderr)
                failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
