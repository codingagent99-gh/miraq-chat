"""
Product API routes — dedicated REST endpoints for product detail lookup.
"""

import logging
from flask import Blueprint, jsonify

from app_config import WOO_BASE_URL
from woo_client import woo_client
from models import WooAPICall
from formatters import format_product, format_custom_product

logger = logging.getLogger("miraq_chat")

products_bp = Blueprint("products", __name__)

@products_bp.route("/products/<int:product_id>", methods=["GET"])
def get_product(product_id: int):
    """
    Fetch a single product by ID from WooCommerce and return it
    in the same clean format used by the chat endpoint.
    """

    base_url = WOO_BASE_URL.rstrip("/")

    api_call = WooAPICall(
        method="GET",
        endpoint=f"{base_url}/products/{product_id}",
        params={},
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

@products_bp.route("/products/<int:product_id>/similar", methods=["GET"])
def get_similar_products(product_id: int):
    """
    Returns cross-sell products ("Pairing It With") if available,
    falling back to related products ("You May Also Like").
    Fetches all similar products in a single WC API call via ?include=
    """
    base_url = WOO_BASE_URL.rstrip("/")

    # Step 1: fetch the source product to get cross_sell_ids / related_ids
    api_call = WooAPICall(
        method="GET",
        endpoint=f"{base_url}/products/{product_id}",
        params={},
        description=f"REST: Fetch product id={product_id} for similar lookup",
    )
    result = woo_client.execute(api_call)

    if not result.get("success"):
        return jsonify({"success": False, "error": "WooCommerce API Request Failed"}), 502

    raw = result.get("data")
    if not raw:
        return jsonify({"success": False, "error": "Product not found"}), 404
    if isinstance(raw, list):
        raw = raw[0]

    cross_sell_ids = raw.get("cross_sell_ids") or []
    related_ids    = raw.get("related_ids") or []

    source_ids = cross_sell_ids if cross_sell_ids else related_ids
    source     = "cross_sell" if cross_sell_ids else "related"

    if not source_ids:
        return jsonify({"success": True, "products": [], "source": source}), 200

    # Step 2: fetch all similar products in one request using ?include=
    bulk_call = WooAPICall(
        method="GET",
        endpoint=f"{base_url}/products",
        params={"include": ",".join(str(i) for i in source_ids), "per_page": len(source_ids)},
        description=f"REST: Fetch similar products for id={product_id}",
    )
    bulk_result = woo_client.execute(bulk_call)

    if not bulk_result.get("success"):
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