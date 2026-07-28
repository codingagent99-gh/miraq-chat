"""
routes/shopify.py — Shopify-specific endpoints for the widget frontend.
Not admin-protected — called directly by the chat widget for logged-in customers.
"""

import requests
from flask import Blueprint, jsonify, request
from chat_logger import get_logger
from store_loader.config import SHOPIFY_STORE_DOMAIN
from models.shopify_token import ShopifyToken
from ecommerce.shopify_endpoints import ShopifyEndpoints
from ecommerce.shopify_proxy import resolve_shopify_customer_id
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