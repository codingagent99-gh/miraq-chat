"""
routes/shopify.py — Shopify-specific endpoints for the widget frontend.
Not admin-protected — called directly by the chat widget for logged-in customers.
"""

import json
import requests
from flask import Blueprint, jsonify, request
from chat_logger import get_logger
from store_loader.config import SHOPIFY_STORE_DOMAIN
from models import db
from models.shopify_token import ShopifyToken
from models.shopify_order_confirmation import ShopifyOrderConfirmation
from ecommerce.shopify_endpoints import ShopifyEndpoints
from ecommerce.shopify_proxy import resolve_shopify_customer_id, verify_events_hmac
from app_config import SHOPIFY_CUSTOMER_AUTH, SHOPIFY_PROXY_MAX_AGE
from store_loader.config import SHOPIFY_CLIENT_SECRET

logger = get_logger("miraq_chat")
shopify_bp = Blueprint("shopify", __name__)

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
    customer_id, proxy_error = resolve_shopify_customer_id(
        request.args,
        mode=SHOPIFY_CUSTOMER_AUTH,
        client_secret=SHOPIFY_CLIENT_SECRET,
        # Only consulted in the development-only insecure mode.
        claimed_customer_id=request.args.get("customer_id", "").strip(),
        max_age_seconds=SHOPIFY_PROXY_MAX_AGE or None,
    )

    if proxy_error:
        logger.error(f"customer-addresses: proxy verification failed ({proxy_error})")
        return jsonify({"error": "unverified_request"}), 403

    if not customer_id:
        # Guest — not an error, simply nothing saved to offer.
        return jsonify({"addresses": [], "default_address_id": None})

    # Retrieve the stored Admin API token
    token_row = ShopifyToken.query.get(SHOPIFY_STORE_DOMAIN)
    if not token_row or token_row.is_expired:
        logger.error("customer-addresses: Shopify Admin token missing or expired")
        return jsonify({"error": "Shopify token unavailable"}), 503

    # Convert numeric ID → GID
    customer_gid = f"gid://shopify/Customer/{customer_id}"

    try:
        resp = requests.post(
            f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-10/graphql.json",
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
    header_hmac = request.headers.get("Shopify-Hmac-Sha256")

    ok, reason = verify_events_hmac(raw_body, header_hmac, SHOPIFY_CLIENT_SECRET)
    if not ok:
        logger.warning(f"shopify events: rejected /events/product-update delivery | reason={reason}")
        return jsonify({"error": "unverified_request"}), 401

    delivery_id = request.headers.get("Shopify-Webhook-Id", "")
    shop_domain = request.headers.get("Shopify-Shop-Domain", "")
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
    header_hmac = request.headers.get("Shopify-Hmac-Sha256")

    ok, reason = verify_events_hmac(raw_body, header_hmac, SHOPIFY_CLIENT_SECRET)
    if not ok:
        logger.warning(f"shopify events: rejected /events/order-paid delivery | reason={reason}")
        return jsonify({"error": "unverified_request"}), 401

    delivery_id = request.headers.get("Shopify-Webhook-Id", "")
    shop_domain = request.headers.get("Shopify-Shop-Domain", "")

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