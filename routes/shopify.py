"""
routes/shopify.py — Shopify-specific endpoints for the widget frontend.
Not admin-protected — called directly by the chat widget for logged-in customers.

Tenant resolution here is NOT the X-MiraQ-License-Id scheme the rest of the
app uses (see store_registry.py) — these are Shopify-native mechanisms that
already carry their own signed tenant identifier:
  - /customer-addresses is an App Proxy request: Shopify appends a `shop`
    query parameter, which is part of what the request's `signature` covers.
  - /events/* are Events API webhook deliveries: Shopify sends a
    Shopify-Shop-Domain header alongside the body-HMAC `signature`.
In both cases the identifier is UNVERIFIED on its own — it only decides
WHOSE secret to check the signature against. The signature check that
follows is what actually proves the request (and that identifier) is
genuine; a request naming a real tenant's domain with a forged signature
still fails. All three routes are listed in store_registry._EXEMPT_PATHS
for exactly this reason — they resolve their own tenant, so they must not
also be gated behind a header they'll never receive (Shopify doesn't know
about X-MiraQ-License-Id).
"""

import json
import re
from datetime import datetime, timezone
import requests
from flask import Blueprint, jsonify, request
from chat_logger import get_logger
from models import db
from models.shopify_token import ShopifyToken
from models.shopify_order_confirmation import ShopifyOrderConfirmation
from ecommerce.shopify_endpoints import ShopifyEndpoints
from ecommerce.shopify_proxy import resolve_shopify_customer_id, verify_events_hmac
from app_config import (
    SHOPIFY_CUSTOMER_AUTH,
    SHOPIFY_PROXY_MAX_AGE,
    SHOPIFY_CLIENT_SECRET,
)

logger = get_logger("miraq_chat")
shopify_bp = Blueprint("shopify", __name__)


def _shopify_header(name: str) -> str:
    """
    Read a Shopify delivery header under either naming scheme.

    Classic webhooks — which is what app/uninstalled is, declared under
    [webhooks] in the app toml — send X-Shopify-Hmac-Sha256,
    X-Shopify-Shop-Domain, X-Shopify-Webhook-Id and X-Shopify-Triggered-At.
    These handlers read only the unprefixed names used by the newer Events
    deliveries, so every real app/uninstalled delivery arrived looking unsigned
    ("missing_hmac_header", shop='') and was rejected, and Shopify kept
    retrying it. The prefixed name is tried first because it is the documented
    one for [webhooks]; the unprefixed one keeps the Events deliveries working.
    """
    return request.headers.get(f"X-{name}") or request.headers.get(name) or ""


def _resolve_tenant_by_shopify_domain(domain: str):
    """
    Look up the Tenant whose shopify_domain matches `domain`.

    `domain` is UNVERIFIED at this point (an App Proxy query param or a
    webhook header — either one is attacker-suppliable on its own). This
    lookup only decides whose secret to verify the request's signature
    against; it is not itself the authentication step. Callers must still
    run verify_app_proxy_signature / verify_events_hmac against the
    resolved tenant's own secret before trusting anything else in the
    request.
    """
    from models import Tenant
    domain = (domain or "").strip()
    if not domain:
        return None
    return Tenant.query.filter_by(shopify_domain=domain).first()

_CUSTOMER_ADDRESSES_QUERY = """
query GetCustomerAddresses($id: ID!) {
  customer(id: $id) {
    id
    firstName
    lastName
    email
    defaultAddress {
      id
      firstName
      lastName
      address1
      address2
      city
      province
      zip
      country
      phone
    }
    addresses(first: 10) {
      id
      firstName
      lastName
      address1
      address2
      city
      province
      zip
      country
      phone
    }
  }
}
"""


@shopify_bp.route("/customer-addresses", methods=["GET"])
def get_customer_addresses():
    """
    Returns saved addresses for the CURRENTLY AUTHENTICATED Shopify customer.
    Called by ShopifyCheckoutPanel on mount to pre-fill the shipping form.

    Identity comes from Shopify's signed App Proxy parameters, never from the
    request: this endpoint returns names, phone numbers and postal addresses,
    so honouring a caller-supplied ``customer_id`` (as it previously did) let
    anyone enumerate the store's customer PII.
    """
    shop = request.args.get("shop", "").strip()
    tenant = _resolve_tenant_by_shopify_domain(shop)
    if not tenant:
        logger.error(f"customer-addresses: no tenant found for shop={shop!r}")
        return jsonify({"error": "unverified_request"}), 403

    # App-level secret: one value for every store. The tenant lookup above
    # only decides WHICH tenant the request claims to be; this key is what
    # proves the claim, and Shopify signs every store's traffic with it.
    client_secret = SHOPIFY_CLIENT_SECRET

    customer_id, proxy_error = resolve_shopify_customer_id(
        request.args,
        mode=SHOPIFY_CUSTOMER_AUTH,
        client_secret=client_secret,
        # Only consulted in the development-only insecure mode.
        claimed_customer_id=request.args.get("customer_id", "").strip(),
        max_age_seconds=SHOPIFY_PROXY_MAX_AGE or None,
    )

    if proxy_error:
        logger.error(f"customer-addresses: proxy verification failed ({proxy_error}) | shop={shop!r}")
        return jsonify({"error": "unverified_request"}), 403

    if not customer_id:
        # Guest — not an error, simply nothing saved to offer.
        return jsonify({"addresses": [], "default_address_id": None})

    # Retrieve the stored Admin API token — tenant.shopify_domain, not the
    # raw shop param, now that the tenant lookup + signature check above has
    # established which tenant this genuinely is.
    token_row = ShopifyToken.query.get(tenant.shopify_domain)
    if not token_row or token_row.is_expired:
        logger.error(f"customer-addresses: Shopify Admin token missing or expired | tenant={tenant.license_id!r}")
        return jsonify({"error": "Shopify token unavailable"}), 503

    # Convert numeric ID → GID
    customer_gid = f"gid://shopify/Customer/{customer_id}"

    try:
        resp = requests.post(
            f"https://{tenant.shopify_domain}/admin/api/2024-10/graphql.json",
            json={"query": _CUSTOMER_ADDRESSES_QUERY, "variables": {"id": customer_gid}},
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token_row.access_token,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"customer-addresses: Shopify Admin API call failed — {e}")
        return jsonify({"error": "Failed to fetch addresses"}), 502

    customer_raw = (data.get("data") or {}).get("customer")
    if not customer_raw:
        return jsonify({"addresses": [], "default_address_id": None})

    # Normalise using the existing parser
    parsed = ShopifyEndpoints().parse_customer(customer_raw)

    # Build the response the frontend expects
    default_id = (customer_raw.get("defaultAddress") or {}).get("id")

    addresses = []
    for raw_addr, norm_addr in zip(
        customer_raw.get("addresses") or [],
        parsed["addresses"],
    ):
        addresses.append({
            "id": raw_addr.get("id", ""),
            "isDefault": raw_addr.get("id") == default_id,
            "firstName": raw_addr.get("firstName", ""),
            "lastName": raw_addr.get("lastName", ""),
            "phone": raw_addr.get("phone", ""),
            "company": raw_addr.get("company", ""),
            "address1": norm_addr["address_1"],
            "address2": norm_addr["address_2"],
            "city": norm_addr["city"],
            "province": norm_addr["state"],
            "zip": norm_addr["postcode"],
            "country": norm_addr["country"],
        })

    return jsonify({
        "addresses": addresses,
        "default_address_id": default_id,
    })


@shopify_bp.route("/events/product-update", methods=["POST"])
def shopify_product_update_event():
    """Minimal receiver for the Product/update Events subscription declared in
    shopify.app.miraq-commerce-agent.toml.

    Nothing in the app currently NEEDS this data — it exists purely to give
    ``shopify app deploy`` a real, working endpoint instead of a stub that
    would fail every delivery. Right now it does exactly one thing: verify
    the delivery is genuinely from Shopify, log it, and acknowledge.

    Deliberately not idempotency-guarded yet: with no side effects, receiving
    the same delivery twice is harmless. If this grows into something that
    actually acts on the payload (e.g. invalidating a cached product ahead of
    StoreLoader's 6-hourly refresh), de-dupe on the Shopify-Webhook-Id header
    before doing so.
    """
    raw_body = request.get_data()  # must be the exact bytes Shopify signed —
    # request.json / request.get_json() re-serializes and would break this.
    header_hmac = _shopify_header("Shopify-Hmac-Sha256") or None
    shop_domain = _shopify_header("Shopify-Shop-Domain")

    tenant = _resolve_tenant_by_shopify_domain(shop_domain)
    if not tenant:
        logger.warning(f"shopify events: no tenant found for shop={shop_domain!r} on /events/product-update")
        return jsonify({"error": "unverified_request"}), 401

    # App-level secret: one value for every store. The tenant lookup above
    # only decides WHICH tenant the request claims to be; this key is what
    # proves the claim, and Shopify signs every store's traffic with it.
    client_secret = SHOPIFY_CLIENT_SECRET
    ok, reason = verify_events_hmac(raw_body, header_hmac, client_secret)
    if not ok:
        logger.warning(f"shopify events: rejected /events/product-update delivery | reason={reason} | shop={shop_domain!r}")
        return jsonify({"error": "unverified_request"}), 401

    delivery_id = _shopify_header("Shopify-Webhook-Id")
    logger.info(
        f"shopify events: Product/update delivery accepted | "
        f"delivery_id={delivery_id!r} shop={shop_domain!r}"
    )

    return jsonify({"received": True}), 200


def _extract_note_attribute(payload: dict, key: str) -> str | None:
    """Pull a single note-attribute value out of an Order payload.

    Payload shape for the unstable Events API's Order topic is NOT confirmed
    against real Shopify deliveries yet (see the TOML comment for this
    subscription) — REST webhooks use snake_case `note_attributes: [{name,
    value}]`; the newer per-topic Events API tends to mirror the Admin
    GraphQL schema, which would be camelCase `noteAttributes`. Both are
    checked so this survives whichever it turns out to be. If neither is
    present, the raw top-level keys are logged so the actual shape can be
    confirmed from the first real delivery and this can be trimmed down.
    """
    for field_name in ("note_attributes", "noteAttributes"):
        attrs = payload.get(field_name)
        if not attrs:
            continue
        for attr in attrs:
            if attr.get("name") == key:
                return attr.get("value")
    return None


def _order_is_paid(payload: dict) -> bool:
    """True if the payload's financial status indicates payment succeeded.

    Same shape uncertainty as _extract_note_attribute above — checks both
    the REST snake_case and GraphQL-style camelCase field/value casing.
    """
    status = payload.get("financial_status") or payload.get("displayFinancialStatus") or ""
    return str(status).strip().lower() == "paid"


@shopify_bp.route("/events/order-paid", methods=["POST"])
def shopify_order_paid_event():
    """Receiver for the Order/update Events subscription (filtered to paid
    orders in-handler — see the TOML comment on this subscription for why
    there's no dedicated "paid" action here).

    Correlates the order back to the widget session via a `miraq_session_id`
    note attribute, which the frontend sets as a cart attribute
    (platform/shopify/useCheckout.ts: prefillAndRedirect) before handing off
    to Shopify's hosted checkout — cart attributes carry through to the
    resulting order's note attributes automatically.

    Writes a ShopifyOrderConfirmation row for /chat/order-status to pick up
    on the widget's next poll, rather than writing the chat Message directly
    here — keeps "did we tell the shopper yet" as a single flag the polling
    route owns, so a retried webhook delivery can't double-post the message.
    """
    raw_body = request.get_data()
    header_hmac = _shopify_header("Shopify-Hmac-Sha256") or None
    shop_domain = _shopify_header("Shopify-Shop-Domain")

    tenant = _resolve_tenant_by_shopify_domain(shop_domain)
    if not tenant:
        logger.warning(f"shopify events: no tenant found for shop={shop_domain!r} on /events/order-paid")
        return jsonify({"error": "unverified_request"}), 401

    # App-level secret: one value for every store. The tenant lookup above
    # only decides WHICH tenant the request claims to be; this key is what
    # proves the claim, and Shopify signs every store's traffic with it.
    client_secret = SHOPIFY_CLIENT_SECRET
    ok, reason = verify_events_hmac(raw_body, header_hmac, client_secret)
    if not ok:
        logger.warning(f"shopify events: rejected /events/order-paid delivery | reason={reason} | shop={shop_domain!r}")
        return jsonify({"error": "unverified_request"}), 401

    if tenant.status == "archived":
        return jsonify({"received": True, "skipped": "tenant_archived"}), 200
    
    from store_registry import bind_tenant_db
    bind_tenant_db(tenant)

    delivery_id = _shopify_header("Shopify-Webhook-Id")

    try:
        payload = json.loads(raw_body)
    except Exception as e:
        logger.error(
            f"shopify events: /events/order-paid — could not parse body | "
            f"delivery_id={delivery_id!r} error={e}"
        )
        return jsonify({"error": "bad_payload"}), 400

    if not _order_is_paid(payload):
        # Order/update fires on every change, not just payment — silently
        # accept and skip the ones we don't care about.
        return jsonify({"received": True, "skipped": "not_paid"}), 200

    session_id = _extract_note_attribute(payload, "miraq_session_id")
    if not session_id:
        logger.warning(
            f"shopify events: /events/order-paid — no miraq_session_id note "
            f"attribute | delivery_id={delivery_id!r} shop={shop_domain!r} "
            f"payload_keys={sorted(payload.keys())}"
        )
        return jsonify({"received": True, "skipped": "no_session_id"}), 200

    order_id = str(payload.get("id") or payload.get("admin_graphql_api_id") or "")
    order_number = str(payload.get("name") or payload.get("order_number") or "") or None

    try:
        row = db.session.get(ShopifyOrderConfirmation, session_id)
        if row is None:
            row = ShopifyOrderConfirmation(session_id=session_id)
            db.session.add(row)
        row.order_id = order_id
        row.order_number = order_number
        # Deliberately NOT resetting delivered=False on an update — if this
        # session already got its confirmation message, a retried/duplicate
        # delivery for the same order shouldn't re-trigger it.
        db.session.commit()
    except Exception as e:
        logger.error(f"shopify events: /events/order-paid — DB write failed: {e}")
        db.session.rollback()
        return jsonify({"error": "db_write_failed"}), 500

    logger.info(
        f"shopify events: Order/update (paid) delivery accepted | "
        f"delivery_id={delivery_id!r} shop={shop_domain!r} "
        f"session_id={session_id!r} order_id={order_id!r}"
    )

    return jsonify({"received": True}), 200

@shopify_bp.route("/events/app-uninstalled", methods=["POST"])
def shopify_app_uninstalled():
    """
    Shopify app/uninstalled webhook — the Shopify equivalent of the WordPress
    plugin's uninstall.php.

    Declared in shopify.app.miraq-commerce-agent.toml under
    [[webhooks.subscriptions]]. Without this route Shopify POSTs into a 404 on
    every uninstall, which means the tenant row and its physical database
    survive forever and RefreshScheduler keeps polling a store whose token
    Shopify has already revoked — a 401 on every tick, indefinitely.

    IMPORTANT: Shopify revokes the access token BEFORE sending this webhook,
    so no Admin API call can succeed from here. Teardown must be purely local.

    Verification differs from the WordPress path by necessity. /deactivate-tenant
    verifies a licence payload signed by the licensing server; Shopify has no
    such payload, so the proof is the body HMAC over the exact delivered bytes,
    keyed with the app-level client secret. The Shopify-Shop-Domain header only
    says which tenant the delivery CLAIMS to be for — the HMAC is what proves
    the delivery came from Shopify at all.

    Always returns 2xx once the request is authenticated, including when the
    tenant is already gone. Shopify retries non-2xx deliveries with backoff for
    days, and "no such tenant" means the work is already done, not that it
    failed. Only a genuine teardown error returns 500 so the retry is useful.
    """
    raw_body = request.get_data()  # exact signed bytes — request.get_json()
    # re-serialises and would break the HMAC.
    header_hmac = _shopify_header("Shopify-Hmac-Sha256") or None
    shop_domain = _shopify_header("Shopify-Shop-Domain")
    delivery_id = _shopify_header("Shopify-Webhook-Id")

    # Authenticate FIRST, before any tenant lookup. Verifying the signature
    # before touching the database means an unsigned request gets an identical
    # 401 whether or not it named a real store, so this cannot be used to
    # enumerate which shops have the app installed.
    ok, reason = verify_events_hmac(raw_body, header_hmac, SHOPIFY_CLIENT_SECRET)
    if not ok:
        logger.warning(
            f"shopify events: rejected /events/app-uninstalled delivery | "
            f"reason={reason} | shop={shop_domain!r} delivery_id={delivery_id!r}"
        )
        return jsonify({"error": "unverified_request"}), 401

    tenant = _resolve_tenant_by_shopify_domain(shop_domain)
    if tenant is None:
        # Authenticated but unknown: a store that never completed provisioning,
        # or a repeat delivery after a successful teardown. Nothing to do.
        logger.info(
            f"shopify events: app/uninstalled for unknown shop={shop_domain!r} "
            f"— nothing to tear down | delivery_id={delivery_id!r}"
        )
        return jsonify({"received": True, "status": "not_found"}), 200

    if tenant.status == "archived":
        logger.info(
            f"shopify events: app/uninstalled — tenant already archived | "
            f"shop={shop_domain!r} delivery_id={delivery_id!r}"
        )
        return jsonify({"received": True, "status": "archived"}), 200

    # A delivery for an uninstall that happened BEFORE the store's current
    # install must not tear that install down. Shopify retries a failed
    # delivery for up to 48 hours, so an uninstall that was rejected (as every
    # one was, before the header fix above), followed by a reinstall, is
    # otherwise retried into archiving the brand-new tenant. The OAuth callback
    # stamps ShopifyToken.fetched_at at install time, which is exactly the
    # moment to compare against.
    triggered_at_raw = _shopify_header("Shopify-Triggered-At")
    token_row = db.session.get(ShopifyToken, tenant.shopify_domain)
    if triggered_at_raw and token_row is not None and token_row.fetched_at is not None:
        try:
            # Shopify sends nanoseconds ("...06:12:02.066047170Z"); Python's
            # fromisoformat accepts at most microseconds, so this raised and the
            # guard fell open. Trim the fraction to 6 digits first.
            ts = triggered_at_raw.strip().replace("Z", "+00:00")
            ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
            triggered_at = datetime.fromisoformat(ts)
            if triggered_at.tzinfo is None:
                triggered_at = triggered_at.replace(tzinfo=timezone.utc)
            installed_at = token_row.fetched_at
            if installed_at.tzinfo is None:
                installed_at = installed_at.replace(tzinfo=timezone.utc)
            if triggered_at < installed_at:
                logger.info(
                    f"shopify events: app/uninstalled predates the current install — "
                    f"ignored | shop={shop_domain!r} triggered_at={triggered_at_raw} "
                    f"installed_at={installed_at.isoformat()} delivery_id={delivery_id!r}"
                )
                return jsonify({"received": True, "status": "stale_ignored"}), 200
        except ValueError:
            logger.warning(
                f"shopify events: unparseable X-Shopify-Triggered-At={triggered_at_raw!r} "
                f"— processing the uninstall | shop={shop_domain!r}"
            )

    # Drop the stored Admin API token before teardown. Shopify has already
    # revoked it, so it is now a dead credential sitting in the control-plane
    # DB; and ShopifyTokenManager would otherwise keep trying to refresh it.
    try:
        ShopifyToken.query.filter_by(store_domain=tenant.shopify_domain).delete()
        db.session.commit()
        logger.info(f"shopify events: token row deleted | shop={shop_domain!r}")
    except Exception as e:
        # Non-fatal: the token is already useless. Roll back so the session is
        # clean for the teardown's own commit.
        db.session.rollback()
        logger.error(
            f"shopify events: failed to delete token row | shop={shop_domain!r} | {e}",
            exc_info=True,
        )

    from routes.deactivation import teardown_tenant
    result = teardown_tenant(tenant, log_prefix="app-uninstalled")
    if not result["success"]:
        # 500 so Shopify retries — teardown is idempotent, and the
        # already-archived check above short-circuits a successful retry.
        logger.error(
            f"shopify events: teardown failed | shop={shop_domain!r} | {result['error']}"
        )
        return jsonify({"error": result["error"]}), 500

    logger.info(
        f"shopify events: app/uninstalled teardown complete | "
        f"shop={shop_domain!r} delivery_id={delivery_id!r}"
    )
    return jsonify({"received": True, "status": "archived"}), 200