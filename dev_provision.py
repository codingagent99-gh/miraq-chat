#!/usr/bin/env python3
"""dev_provision.py — provision a local-dev tenant against a real store,
bypassing license signature verification (LICENSE_VERIFICATION_ENABLED=false).

Provisioning is a TWO-STEP handshake and this script does both.

  1. POST /provision-tenant
     Creates the tenant row and kicks off a background build.

  2. POST /webhooks/woocommerce/<license_id>/catalog-push
     Sends the catalog.

Step 2 is not optional. The backend runs in PUSH-ONLY mode: StoreLoader's live
pull is disabled in code (store_loader/__init__.py — "Live pull disabled ... 
awaiting push"), so with no push it loads an empty catalog, the build reports
0 products, and the tenant lands in status=provision_failed. Nothing recovers
it on its own, because the recovery path IS the push.

In production the WordPress plugin does step 2 for us —
class-provisioning.php calls MiraQ_Catalog_Push::full_push() straight after a
successful provision. Running this script against a backend with no plugin
installed means nobody makes that call, which is why provisioning "succeeds"
(HTTP 200) and the tenant is still broken.

WHY THE PUSH EXISTS AT ALL
──────────────────────────
Some hosts' WAFs block backend-initiated requests to WooCommerce's REST API,
so the backend cannot pull. The plugin gathers the catalog via an internal
REST dispatch that never leaves its own server and POSTs the result in.

This script has no such trick — it pulls over the public REST API from
wherever you run it. That normally works from a dev machine, since the pull is
disabled by code rather than because it fails. But on a store whose WAF blocks
outside REST access, the gather step will fail and there is no way around it
from here; you need the plugin.

ABORT-ON-PARTIAL
────────────────
If any page of any resource errors mid-pagination, the whole push is abandoned
rather than sent short. A truncated catalog looks perfectly valid to the
backend and would overwrite the last good snapshot with a shorter one — the
plugin takes the same position, deliberately.
"""
import argparse
import html
import json
import os
import sys

import requests

DEFAULT_EXPIRES_AT = "2027-01-01T00:00:00Z"
PER_PAGE = 100
GATHER_TIMEOUT = 30
PUSH_TIMEOUT = 120  # a full catalog is large; the plugin allows 45s for a LAN hop

# requests sends "python-requests/2.x" by default, and a stock ModSecurity CRS
# rule (913100, "Found User-Agent associated with scripting/generic HTTP client")
# blocks that outright with a 406 — before it ever looks at the credentials.
# The request is authenticated and authorised; the UA string alone is what the
# rule matches on. Sending a normal browser UA is not a bypass of any access
# control, it just avoids tripping a rule aimed at unattended scrapers.
#
# It only helps when the UA rule is what fired. A WAF blocking /wp-json by path,
# by IP reputation, or by rate will still block, and no header changes that —
# use the WordPress plugin for those.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _session(user_agent):
    """One session for the whole gather — keeps the connection alive and
    applies the UA to every request, including the paginated follow-ups."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        # Some WAF rules also flag a missing/odd Accept on API paths.
        "Accept": "application/json",
    })
    return s


class GatherError(RuntimeError):
    """A page failed mid-pagination — abort rather than push a short catalog."""


def _wc_get_all(sess, wc_base, auth, route, params=None):
    """Fetch every page of a WooCommerce REST collection.

    Raises GatherError on any non-200 so the caller can abandon the push.
    Returns a list.
    """
    items = []
    page = 1
    while True:
        query = dict(params or {})
        query.update({"page": page, "per_page": PER_PAGE})
        url = f"{wc_base}{route}"
        resp = sess.get(url, params=query, auth=auth, timeout=GATHER_TIMEOUT)

        if resp.status_code != 200:
            hint = ""
            if resp.status_code in (403, 406) or "Mod_Security" in (resp.text or ""):
                hint = (
                    "\n    A WAF rejected this before WooCommerce saw it. Try a "
                    "different --user-agent, or whitelist your IP in the host's "
                    "ModSecurity settings. If neither works, use the plugin."
                )
            raise GatherError(
                f"{route} page {page} returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}{hint}"
            )

        try:
            batch = resp.json()
        except ValueError as exc:
            raise GatherError(f"{route} page {page} returned unparseable JSON: {exc}")

        if not isinstance(batch, list):
            raise GatherError(f"{route} page {page} returned {type(batch).__name__}, expected a list")

        if not batch:
            break

        items.extend(batch)

        # Trust the header when present; fall back to a short page meaning "last".
        total_pages = resp.headers.get("X-WP-TotalPages")
        if total_pages and total_pages.isdigit():
            if page >= int(total_pages):
                break
        elif len(batch) < PER_PAGE:
            break

        page += 1

    return items


def _build_attributes(sess, wc_base, auth):
    """Attributes with their terms, in the shape class-catalog-push.php sends.

    The plugin reads WordPress objects directly, so the field names below are
    its names, not the REST API's — the backend parses THIS shape:

        attribute_name  hyphenated, no pa_ prefix  ("quick-ship")
        attribute_label human-readable             ("Quick Ship")
        taxonomy        the pa_-prefixed taxonomy  ("pa_quick-ship")

    REST gives us `slug` (already "pa_quick-ship") and `name` ("Quick Ship"),
    so attribute_name is derived by stripping the prefix. Getting this wrong
    is silent: the catalog loads and attribute matching just never fires.
    """
    raw = _wc_get_all(sess, wc_base, auth, "/products/attributes")
    out = []
    for attr in raw:
        slug = str(attr.get("slug") or "")
        taxonomy = slug if slug.startswith("pa_") else f"pa_{slug}"
        attribute_name = taxonomy[3:]  # strip "pa_"

        # hide_empty=false: the plugin uses get_terms(hide_empty => false), and
        # a term with no products still needs to be in the vocabulary so the
        # classifier can recognise the word and answer "nothing in that".
        terms = _wc_get_all(
            sess, wc_base, auth,
            f"/products/attributes/{attr['id']}/terms",
            {"hide_empty": "false"},
        )
        out.append({
            "attribute_id": int(attr.get("id") or 0),
            "attribute_name": attribute_name,
            "attribute_label": attr.get("name") or attribute_name,
            "taxonomy": taxonomy,
            "terms": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "slug": t.get("slug"),
                    "count": t.get("count", 0),
                }
                for t in terms
            ],
        })
    return out


def _currency_symbol(sess, wc_base, auth):
    """Store currency symbol. Falls back to '$' — the same default StoreLoader
    uses — rather than failing the push over a cosmetic field."""
    try:
        resp = sess.get(
            f"{wc_base}/data/currencies/current", auth=auth, timeout=GATHER_TIMEOUT
        )
        if resp.status_code == 200:
            # WooCommerce returns the symbol HTML-encoded — "&#36;" for USD,
            # "&#8377;" for INR. Nothing downstream decodes it: StoreLoader
            # stores currency_symbol verbatim and app_config.get_currency_symbol()
            # hands it straight to the formatters, so an encoded symbol shows up
            # literally as "&#36;12.00" on every price in the widget.
            return html.unescape(resp.json().get("symbol") or "") or "$"
    except Exception:
        pass
    print("  ! could not read store currency — defaulting to '$'")
    return "$"


def gather_catalog(wp_base_url, woo_key, woo_secret, user_agent=DEFAULT_USER_AGENT):
    """Assemble the push payload. Raises GatherError rather than returning partial data."""
    wc_base = f"{wp_base_url.rstrip('/')}/wp-json/wc/v3"
    auth = (woo_key, woo_secret)
    sess = _session(user_agent)

    print(f"  fetching products   from {wc_base} ...")
    products = _wc_get_all(sess, wc_base, auth, "/products", {"status": "publish"})
    print(f"    {len(products)} products")

    print("  fetching categories ...")
    categories = _wc_get_all(sess, wc_base, auth, "/products/categories", {"hide_empty": "true"})
    print(f"    {len(categories)} categories")

    print("  fetching tags ...")
    tags = _wc_get_all(sess, wc_base, auth, "/products/tags", {"hide_empty": "true"})
    print(f"    {len(tags)} tags")

    print("  fetching attributes + terms ...")
    attributes = _build_attributes(sess, wc_base, auth)
    term_count = sum(len(a["terms"]) for a in attributes)
    print(f"    {len(attributes)} attributes / {term_count} terms")

    return {
        "categories": categories,
        "tags": tags,
        "products": products,
        "all_attributes_raw": attributes,
        "currency_symbol": _currency_symbol(sess, wc_base, auth),
    }


def push_catalog(backend, license_id, woo_key, woo_secret, payload):
    """POST the catalog to the same route the plugin uses.

    Auth is the tenant's own WooCommerce key/secret, compared timing-safely
    against the stored pair (see webhook_routes._verify_credentials), so these
    must be the SAME credentials provisioning was given.
    """
    url = f"{backend.rstrip('/')}/webhooks/woocommerce/{license_id}/catalog-push"
    resp = requests.post(
        url,
        json=payload,
        headers={
            # Required: this route is not in store_registry._EXEMPT_PATHS, so
            # the tenant-resolution middleware runs first and needs the header.
            "X-MiraQ-License-Id": license_id,
            "X-Consumer-Key": woo_key,
            "X-Consumer-Secret": woo_secret,
        },
        timeout=PUSH_TIMEOUT,
    )
    return resp


def main():
    p = argparse.ArgumentParser(
        description="Provision a dev tenant and push its catalog.",
    )
    p.add_argument("--license-id", required=True, help='e.g. dev-geoffg7')
    p.add_argument("--site-domain", required=True)
    p.add_argument("--wp-base-url", required=True)
    p.add_argument("--woo-key", required=True)
    p.add_argument("--woo-secret", default=None,
                   help="or set WOO_SECRET env var instead")
    p.add_argument("--backend", default="http://localhost:5000")
    p.add_argument("--tenant-uuid", default=None,
                   help="optional; exercises the tenant-identity resolution "
                        "path (free->paid upgrade). Omit for a plain new tenant.")
    p.add_argument("--expires-at", default=DEFAULT_EXPIRES_AT)
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                   help="UA sent when gathering the catalog. The default avoids "
                        "ModSecurity's scripting-client rule, which 406s "
                        "requests' own UA before WooCommerce sees the request.")
    p.add_argument("--no-push", action="store_true",
                   help="provision only. The tenant will sit in "
                        "provision_failed until something pushes a catalog.")
    p.add_argument("--push-only", action="store_true",
                   help="skip provisioning; push a catalog to a tenant that "
                        "already exists. Use this to repair a tenant stuck in "
                        "provision_failed without re-provisioning it.")
    args = p.parse_args()

    woo_secret = args.woo_secret or os.environ.get("WOO_SECRET")
    if not woo_secret:
        raise SystemExit("Provide --woo-secret or set WOO_SECRET env var")

    if args.no_push and args.push_only:
        raise SystemExit("--no-push and --push-only are mutually exclusive")

    # ── Step 1: provision ────────────────────────────────────────────────────
    if not args.push_only:
        raw_payload = json.dumps({
            "licenseId": args.license_id,
            "expiresAt": args.expires_at,
        }, separators=(",", ":"))

        body = {
            "raw_payload": raw_payload,
            "signature": "dev-placeholder-unused",
            "site_domain": args.site_domain,
            "wp_base_url": args.wp_base_url,
            "woo_key": args.woo_key,
            "woo_secret": woo_secret,
        }
        # Omitted entirely rather than sent empty: the endpoint logs a warning
        # for a malformed value, and "" is malformed.
        if args.tenant_uuid:
            body["tenant_uuid"] = args.tenant_uuid

        print("[1/2] provisioning ...")
        resp = requests.post(f"{args.backend}/provision-tenant", json=body)
        print(f"  HTTP {resp.status_code} {resp.text[:500]}")

        if resp.status_code != 200:
            raise SystemExit("provisioning failed — not attempting the push")

        if args.no_push:
            print("\n--no-push set. The tenant has no catalog and will report "
                  "status=provision_failed until one is pushed.")
            return

        # The build kicked off by provisioning is EXPECTED to fail here: it runs
        # before any catalog exists, so it sees 0 products and writes
        # status=provision_failed. The push below flips it back to active (see
        # webhook_routes.woocommerce_catalog_push). A provision_failed line in
        # the log at this point is normal, not a problem to chase.
        print("  (a 'loader degraded / provision_failed' error in the backend "
              "log here is expected — the push below clears it)")

    # ── Step 2: catalog push ─────────────────────────────────────────────────
    print("\n[2/2] gathering catalog from WooCommerce ...")
    try:
        payload = gather_catalog(
            args.wp_base_url, args.woo_key, woo_secret, args.user_agent
        )
    except GatherError as exc:
        print(f"\n  ABORTED: {exc}", file=sys.stderr)
        print(
            "\n  Nothing was pushed — a partial catalog would overwrite the "
            "backend's last good snapshot with a shorter one.\n"
            "  If the store's WAF blocks outside REST access, this script "
            "cannot gather the catalog; use the WordPress plugin.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not payload["products"]:
        # The receiving route rejects a payload with no 'products', and a store
        # with nothing published is a real situation worth naming plainly
        # rather than letting it come back as a 400.
        raise SystemExit(
            "  No published products found — nothing to push. "
            "Check that the store has products with status=publish."
        )

    print("\n  pushing ...")
    resp = push_catalog(args.backend, args.license_id, args.woo_key, woo_secret, payload)
    print(f"  HTTP {resp.status_code} {resp.text[:500]}")

    if resp.status_code >= 300:
        raise SystemExit("push rejected — tenant will stay in provision_failed")

    # The route applies the catalog and flips the status in a BACKGROUND thread,
    # so a 200 means "accepted", not "already active". Watch the backend log for
    # "CatalogPush: recovered tenant from provision_failed -> active".
    print(
        "\nAccepted. The catalog is applied on a background thread — look for\n"
        "  'CatalogPush: recovered tenant from provision_failed -> active'\n"
        "in the backend log to confirm the tenant is serving."
    )


if __name__ == "__main__":
    main()