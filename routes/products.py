"""
Product API routes — dedicated REST endpoints for product detail lookup.
"""

import logging
from flask import Blueprint, jsonify, request as flask_request

from woo_client import woo_client

from models import db, Conversation, Message, WooAPICall

from formatters import format_product, format_custom_product
from ecommerce import endpoints

logger = logging.getLogger("miraq_chat")

products_bp = Blueprint("products", __name__)

@products_bp.route("/products/<int:product_id>", methods=["GET"])
def get_product(product_id: int):
    """
    Fetch a single product by ID from WooCommerce and return it
    in the same clean format used by the chat endpoint.
    """

    api_call = endpoints.fetch_product(
        product_id=product_id,
        description=f"REST: Fetch product id={product_id}",
    )

    result = woo_client.execute(api_call)

    if not result.get("success"):
        real_error = result.get("error", "Unknown error from woo_client")
        logger.error(f"WooCommerce API Failed for product {product_id}: {real_error}")
        
        return jsonify({
            "success": False,
            "error": "WooCommerce API Request Failed",
            "real_woo_error": str(real_error),
            "debug_result": result
        }), 502

    raw = result.get("data")
    if not raw or (isinstance(raw, list) and len(raw) == 0):
        return jsonify({
            "success": False,
            "error": "Product not found",
        }), 404

    # WC API returns a dict for single-product lookup
    if isinstance(raw, list):
        raw = raw[0]

    # Format using the existing formatter
    if "featured_image" in raw:
        product = format_custom_product(raw)
    else:
        product = format_product(raw)

    # ── Enrich with detail fields not in the compact formatter ──
    product["description"] = _clean_html(raw.get("description", ""))
    product["short_description"] = _clean_html(raw.get("short_description", ""))
    product["average_rating"] = raw.get("average_rating", "0")
    product["rating_count"] = raw.get("rating_count", 0)
    product["weight"] = raw.get("weight", "")
    product["dimensions"] = raw.get("dimensions", {})
    product["total_sales"] = raw.get("total_sales", 0)
    product["stock_status"] = raw.get("stock_status", "")
    product["stock_quantity"] = raw.get("stock_quantity")

    return jsonify({
        "success": True,
        "product": product,
    }), 200


@products_bp.route("/products/similar/save", methods=["POST"])
def save_similar_message():
    """
    Persists a "Show Similar Products" bot message to the DB so it
    survives page reloads, exactly like _finalize_turn does for chat messages.
    Body: { session_id, text, products, source }
    """
    import uuid as _uuid
    body = flask_request.get_json(silent=True) or {}

    session_id = body.get("session_id")
    text       = body.get("text", "")
    products   = body.get("products", [])

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session_uuid = _uuid.UUID(session_id)
    except ValueError:
        return jsonify({"success": False, "error": "invalid session_id"}), 400

    conversation = Conversation.query.get(session_uuid)
    if not conversation:
        return jsonify({"success": False, "error": "conversation not found"}), 404

    msg = Message(
        conversation_id=conversation.id,
        role="bot",
        content=text,
        intent="similar_products",
        metadata_json={"products": products},
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({"success": True}), 200


@products_bp.route("/products/<int:product_id>/similar", methods=["GET"])
def get_similar_products(product_id: int):
    """
    Returns similar products for a given product ID.
    Priority:
      1. _recommended_products meta  → "Pairing It With"
      2. upsell_ids                  → "You May Also Like"
      3. related_ids                 → "You May Also Like"
    If the product is a variation, fetches the parent first.
    """
    base_url = WOO_BASE_URL.rstrip("/")

    # Step 1: fetch the product
    api_call = WooAPICall(
        method="GET",
        endpoint=f"{base_url}/products/{product_id}",
        params={},
        description=f"REST: Fetch product id={product_id} for similar lookup",
    )
    result = woo_client.execute(api_call)

    if not result.get("success"):
        logger.error(f"WooCommerce API failed for similar lookup, product {product_id}")
        return jsonify({"success": False, "error": "WooCommerce API Request Failed"}), 502

    raw = result.get("data")
    if not raw:
        return jsonify({"success": False, "error": "Product not found"}), 404
    if isinstance(raw, list):
        raw = raw[0]

    # Step 2: if variation, fetch parent instead
    if raw.get("type") == "variation" and raw.get("parent_id"):
        parent_call = WooAPICall(
            method="GET",
            endpoint=f"{base_url}/products/{raw['parent_id']}",
            params={},
            description=f"REST: Fetch parent id={raw['parent_id']} for similar lookup",
        )
        parent_result = woo_client.execute(parent_call)
        if parent_result.get("success") and parent_result.get("data"):
            raw = parent_result["data"]
            if isinstance(raw, list):
                raw = raw[0]

    # Step 3: resolve IDs using priority chain
    meta_data = raw.get("meta_data") or []
    recommended_ids = []
    for meta in meta_data:
        if meta.get("key") == "_recommended_products":
            val = meta.get("value") or []
            recommended_ids = [int(i) for i in val if str(i).isdigit()]
            break

    upsell_ids  = raw.get("upsell_ids") or []
    related_ids = raw.get("related_ids") or []

    if recommended_ids:
        source_ids, source = recommended_ids, "cross_sell"
    elif upsell_ids:
        source_ids, source = upsell_ids, "related"
    else:
        source_ids, source = related_ids, "related"

    if not source_ids:
        return jsonify({"success": True, "products": [], "source": source}), 200

    # Step 4: fetch all similar products in one request
    bulk_call = WooAPICall(
        method="GET",
        endpoint=f"{base_url}/products",
        params={
            "include": ",".join(str(i) for i in source_ids),
            "per_page": len(source_ids),
        },
        description=f"REST: Fetch similar products for id={product_id}",
    )
    bulk_result = woo_client.execute(bulk_call)

    if not bulk_result.get("success"):
        logger.error(f"Failed to fetch similar products for id={product_id}")
        return jsonify({"success": False, "error": "Failed to fetch similar products"}), 502

    items = bulk_result.get("data") or []
    products = []
    for item in items:
        if "featured_image" in item:
            products.append(format_custom_product(item))
        else:
            products.append(format_product(item))

    return jsonify({"success": True, "products": products, "source": source}), 200


def _clean_html(html: str) -> str:
    """Strip HTML tags."""
    import re
    if not html:
        return ""
    clean = re.sub(r'<[^>]+>', '', html)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean