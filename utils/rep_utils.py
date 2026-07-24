"""
handlers/rep_utils.py — Shared rep-facing product utilities.

Imported by both chat.py and variant_handler.py to avoid circular imports.
"""

from woo_client import woo_client
from ecommerce import endpoints
from app_config import BULK_ORDER_ROLES
from chat_logger import get_logger

logger = get_logger("miraq_chat")


def fetch_product_order_history(product_id: int, role: str) -> list:
    """
    Fetch the most recent orders containing product_id.
    Only runs for rep roles — customers don't see other customers' orders.
    Returns a list of raw WooCommerce order dicts (empty on failure).
    """
    if not product_id or role not in BULK_ORDER_ROLES:
        return []
    try:
        call   = endpoints.search_orders_by_product(
            product_id=product_id, per_page=3,
            description=f"Product order history for product_id={product_id}",
        )
        result = woo_client.execute(call)
        if result.get("success") and isinstance(result.get("data"), list):
            return result["data"]
    except Exception as exc:
        logger.warning(f"fetch_product_order_history | error={exc}")
    return []


def _extract_variation_attributes(item: dict) -> list[dict]:
    """Pull human-readable variation attributes (e.g. Size: 12"x24", Color:
    Beige) off a WooCommerce line item's meta_data. Internal/hidden meta
    (keys starting with "_", e.g. "_reduced_stock") is excluded — only
    entries WooCommerce marks with a display_key/display_value are kept.

    Duplicated from handlers/chat_utils.py rather than imported, to avoid
    adding a new cross-module import (this file is deliberately kept free
    of imports from chat_utils/chat.py to sidestep circular imports).
    """
    out = []
    for meta in item.get("meta_data", []) or []:
        key = meta.get("key", "") or ""
        if key.startswith("_"):
            continue
        display_key = meta.get("display_key") or key
        display_value = meta.get("display_value") or meta.get("value")
        if display_key and display_value:
            out.append({"attribute": str(display_key), "value": str(display_value)})
    return out


def format_product_orders_for_action(orders: list) -> list:
    out = []
    for o in orders:
        billing  = o.get("billing", {}) or {}
        email    = billing.get("email", "")
        company  = (
            billing.get("company")
            or f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()
            or email                              # ← use email before "Customer #ID"
            or f"Customer #{o.get('customer_id', '')}"
        )
        date_str = (o.get("date_created") or "")[:10]
        items    = [
            {
                "product_name": item.get("name", ""),
                "product_id":   item.get("product_id"),
                "variation_id": item.get("variation_id") or 0,
                "quantity":     item.get("quantity", 1),
                "variation_attributes": _extract_variation_attributes(item),
            }
            for item in o.get("line_items", [])
        ]
        out.append({
            "order_id":              str(o.get("id", "")),
            "order_number":          str(o.get("number") or o.get("id", "")),
            "date_created":          date_str,
            "customer_id":           str(o.get("customer_id", "")),
            "customer_display_name": company,
            "customer_email":        email,        # ← ADD
            "items":                 items,
        })
    return out