#!/usr/bin/env python3
"""
seed_tenant_one.py — Phase 1 exit step: seed the current single-store
deployment as tenant #1 in the new `tenants` table.

Run once, after `flask db upgrade` has created the table:

    python seed_tenant_one.py --license-id <value> [--dry-run]

--license-id is required and NOT guessed. It must be whatever value the
widget/plugin will send in the X-MiraQ-License-Id header once Phase 3 wires
up register_before_request — the WordPress plugin (class-provisioning.php,
miraQ-chat-widget.php) already stores this as `wc_chat_widget_license_id`,
issued by the Silfra license server on activation. Options:
  - If the plugin has already been activated for this store: use the real
    licenseId from that activation response — check the WP `wc_chat_widget_*`
    options, or the License tab in wp-admin.
  - If it hasn't been activated yet: pass a placeholder (e.g. "dev-wgc") and
    correct this row's license_id once real activation happens, before
    Phase 3 goes live — a mismatch here just means the header lookup 404s,
    it's not silently wrong.

This is a one-time script, not a migration: it reads current app_config /
store_loader.config values, which only exist as single-store globals until
Stage 2. Safe to re-run — it upserts on license_id.
"""
from __future__ import annotations
import argparse
import sys
import urllib.parse

import os

from app_config import STORE_NAME

# Read WooCommerce settings straight from the environment rather than from
# app_config / store_loader.config. Those module globals are being removed as
# the conversion progresses (Phase 2 strips them), and this script is the one
# remaining thing that legitimately needs the pre-multi-store single-store
# values. Reading os.environ keeps it working on a server where the existing
# .env is already in place, exactly as the single-store build did, without
# holding those globals alive for its sake alone.
_ENV_WOO_BASE_URL = os.getenv(
    "WOO_BASE_URL",
    os.getenv("WP_BASE_URL", "") + "/wp-json/wc/v3" if os.getenv("WP_BASE_URL") else "",
)
_ENV_WOO_KEY = os.getenv("WOO_CONSUMER_KEY", "")
_ENV_WOO_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")


def _db_name_from_uri(uri: str) -> str:
    return urllib.parse.urlparse(uri).path.lstrip("/")


def _wp_base_from_woo_base(woo_base_url: str) -> str:
    # WOO_BASE_URL is "<wp_base>/wp-json/wc/v3" — strip the known suffix.
    suffix = "/wp-json/wc/v3"
    if woo_base_url.endswith(suffix):
        return woo_base_url[: -len(suffix)]
    return woo_base_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--license-id", required=True,
        help="The license_id this tenant will be looked up by (X-MiraQ-License-Id).",
    )
    parser.add_argument(
        "--woo-base-url", default=None,
        help="Override WOO_BASE_URL (.../wp-json/wc/v3). Defaults to $WOO_BASE_URL.",
    )
    parser.add_argument(
        "--woo-key", default=None,
        help="Override the WooCommerce consumer key. Defaults to $WOO_CONSUMER_KEY.",
    )
    parser.add_argument(
        "--woo-secret", default=None,
        help="Override the WooCommerce consumer secret. Defaults to $WOO_CONSUMER_SECRET.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written without touching the database.",
    )
    args = parser.parse_args()

    # CLI flag wins over environment, so this can seed a test store's
    # credentials from a laptop without editing the server's .env.
    woo_base_url = args.woo_base_url or _ENV_WOO_BASE_URL
    woo_key      = args.woo_key      or _ENV_WOO_KEY
    woo_secret   = args.woo_secret   or _ENV_WOO_SECRET

    if not woo_base_url:
        print(
            "! No WooCommerce base URL. Set WOO_BASE_URL (or WP_BASE_URL) in the\n"
            "  environment, or pass --woo-base-url https://example.com/wp-json/wc/v3",
            file=sys.stderr,
        )
        sys.exit(1)

    # Imported here, after argparse, so --help doesn't need a DB/app context.
    from server import app  # noqa: reuses the real Flask app's DB config
    from models import db, Tenant
    from tenant_crypto import encrypt_secret

    database_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    db_name = _db_name_from_uri(database_uri)
    wp_base_url = _wp_base_from_woo_base(woo_base_url)

    if not woo_key or not woo_secret:
        print(
            "! WOO_CONSUMER_KEY / WOO_CONSUMER_SECRET are empty in the current "
            "environment — seeding anyway, but woo_client calls will fail "
            "until these are set.",
            file=sys.stderr,
        )

    print(f"Target DB:        {db_name}")
    print(f"license_id:       {args.license_id}")
    print(f"site_domain:      {urllib.parse.urlparse(wp_base_url).hostname}")
    print(f"wp_base_url:      {wp_base_url}")
    print(f"woo_key:          {woo_key!r}")
    print(f"woo_secret:       {'*' * 8 if woo_secret else '(empty)'}")
    print(f"ecommerce_backend: woocommerce")
    print(f"STORE_NAME:       {STORE_NAME}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    with app.app_context():
        existing = Tenant.query.filter_by(license_id=args.license_id).one_or_none()
        if existing:
            tenant = existing
            print(f"\nUpdating existing tenant row (tenant_id={tenant.tenant_id}).")
        else:
            tenant = Tenant(license_id=args.license_id)
            db.session.add(tenant)
            print("\nCreating new tenant row.")

        tenant.plan = tenant.plan or "free"
        tenant.db_name = db_name
        tenant.site_domain = urllib.parse.urlparse(wp_base_url).hostname
        tenant.wp_base_url = wp_base_url
        tenant.woo_key = woo_key
        tenant.woo_secret_encrypted = encrypt_secret(woo_secret)
        tenant.ecommerce_backend = "woocommerce"
        tenant.status = "active"

        db.session.commit()
        print(f"Done. tenant_id={tenant.tenant_id}")


if __name__ == "__main__":
    main()