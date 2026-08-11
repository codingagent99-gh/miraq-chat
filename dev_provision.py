#!/usr/bin/env python3
"""dev_provision.py — provision a local-dev tenant against a real store,
bypassing license signature verification (LICENSE_VERIFICATION_ENABLED=false).
"""
import argparse
import json
import os
import requests

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--license-id", required=True, help='e.g. dev-geoffg7')
    p.add_argument("--site-domain", required=True)
    p.add_argument("--wp-base-url", required=True)
    p.add_argument("--woo-key", required=True)
    p.add_argument("--woo-secret", default=None,
                    help="or set WOO_SECRET env var instead")
    p.add_argument("--backend", default="http://localhost:5000")
    args = p.parse_args()

    woo_secret = args.woo_secret or os.environ.get("WOO_SECRET")
    if not woo_secret:
        raise SystemExit("Provide --woo-secret or set WOO_SECRET env var")

    raw_payload = json.dumps({
        "licenseId": args.license_id,
        "expiresAt": "2027-01-01T00:00:00Z",
    }, separators=(",", ":"))

    resp = requests.post(f"{args.backend}/provision-tenant", json={
        "raw_payload": raw_payload,
        "signature": "dev-placeholder-unused",
        "site_domain": args.site_domain,
        "wp_base_url": args.wp_base_url,
        "woo_key": args.woo_key,
        "woo_secret": woo_secret,
    })
    print(resp.status_code, resp.json())

if __name__ == "__main__":
    main()