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
    BASE = WOO_BASE_URL.rstrip("/") + "/wp-json/wc/v3"

    api_call = WooAPICall(
        method="GET",
        endpoint=f"{BASE}/products/{product_id}",
        params={},
        description=f"REST: Fetch product id={product_id}",
    )

    result = woo_client.execute(api_call)

    if not result.get("success"):
        return jsonify({
            "success": False,
            "error": result.get("error", "Failed to fetch product"),
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


def _clean_html(html: str) -> str:
    """Strip HTML tags."""
    import re
    if not html:
        return ""
    clean = re.sub(r'<[^>]+>', '', html)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean