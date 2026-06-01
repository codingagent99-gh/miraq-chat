"""
api_builder/shopify_order_calls.py — WooAPICall builders for Shopify order intents.

These functions produce WooAPICall objects with surface="shopify_orders".
chat.py._execute_api_calls routes those to ShopifyOrdersExecutor instead of
woo_client, so the rest of the pipeline (handlers, formatters) is untouched.

Called from api_builder/__init__.py order builders when ECOMMERCE_BACKEND=shopify.
"""

from models import WooAPICall


def _customer_gid(customer_id) -> str:
    """Convert a numeric Shopify customer ID to a GID string."""
    s = str(customer_id)
    if s.startswith("gid://"):
        return s
    return f"gid://shopify/Customer/{s}"


# ── Public builders ──────────────────────────────────────────────────────────

def build_order_history_call(
    customer_id,
    page: int = 1,
    per_page: int = 5,
    description: str = "",
    date_after: str = None,
    date_before: str = None,
) -> WooAPICall:
    """
    Build a call to list recent orders for a customer.
    Used by ORDER_HISTORY and HISTORICAL_SEARCH intents.
    """
    body = {
        "_op":          "list_customer_orders",
        "customer_gid": _customer_gid(customer_id),
        "page":         page,
        "per_page":     per_page,
    }
    if date_after:
        body["date_after"] = date_after
    if date_before:
        body["date_before"] = date_before

    return WooAPICall(
        method="GET",
        endpoint="orders",
        params={},
        body=body,
        description=description or f"Shopify: list orders page={page} per_page={per_page}",
        surface="shopify_orders",
    )


def build_last_order_call(
    customer_id,
    description: str = "",
) -> WooAPICall:
    """
    Build a call to fetch the single most recent order for a customer.
    Used by LAST_ORDER and REORDER intents.
    """
    return WooAPICall(
        method="GET",
        endpoint="orders",
        params={},
        body={
            "_op":          "list_customer_orders",
            "customer_gid": _customer_gid(customer_id),
            "page":         1,
            "per_page":     1,
        },
        description=description or "Shopify: fetch last order",
        surface="shopify_orders",
    )


def build_fetch_order_call(
    order_id,
    description: str = "",
) -> WooAPICall:
    """
    Build a call to fetch a single order by ID (numeric or GID).
    Used by ORDER_TRACKING, ORDER_STATUS, and handle_order_detail.
    """
    return WooAPICall(
        method="GET",
        endpoint="orders",
        params={},
        body={
            "_op":      "fetch_order",
            "order_id": str(order_id),
        },
        description=description or f"Shopify: fetch order id={order_id}",
        surface="shopify_orders",
    )


def build_historical_search_call(
    customer_id,
    page: int = 1,
    per_page: int = 20,
    description: str = "",
    date_after: str = None,
    date_before: str = None,
    include_order_ids: list = None,
) -> WooAPICall:
    """
    Build a call to list orders for historical product search.
    Fetches more orders than order_history to give the search a wider pool.
    """
    body = {
        "_op":          "list_customer_orders",
        "customer_gid": _customer_gid(customer_id),
        "page":         page,
        "per_page":     per_page,
    }
    if date_after:
        body["date_after"] = date_after
    if date_before:
        body["date_before"] = date_before
    if include_order_ids:
        body["include"] = [str(i) for i in include_order_ids]

    return WooAPICall(
        method="GET",
        endpoint="orders",
        params={},
        body=body,
        description=description or "Shopify: historical order search",
        surface="shopify_orders",
    )