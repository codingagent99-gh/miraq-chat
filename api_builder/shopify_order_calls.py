"""
api_builder/shopify_order_calls.py — WooAPICall builders for Shopify order intents.

These functions produce WooAPICall objects with surface="shopify_orders".
chat.py._execute_api_calls routes those to ShopifyOrdersExecutor instead of
woo_client, so the rest of the pipeline (handlers, formatters) is untouched.

Called from api_builder/__init__.py order builders when ECOMMERCE_BACKEND=shopify.
"""

from models import WooAPICall

# Placeholder written by the builders when the customer has not been resolved
# yet. Must stay in sync with ShopifyOrdersExecutor's guard.
PLACEHOLDER_CUSTOMER_ID = "CURRENT_USER_ID"


def _customer_gid(customer_id) -> str:
    """Convert a numeric Shopify customer ID to a GID string.

    The ``CURRENT_USER_ID`` placeholder is returned untouched. Wrapping it
    produced ``gid://shopify/Customer/CURRENT_USER_ID``, which silently
    defeated the executor's "not logged in" guard (an equality check against
    the bare placeholder) and sent a malformed GID to Shopify, turning a
    graceful empty result into a GraphQL error.
    """
    s = str(customer_id)
    if s == PLACEHOLDER_CUSTOMER_ID or s.startswith("gid://"):
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
    customer_id=None,
) -> WooAPICall:
    """
    Build a call to fetch a single order by NAME (e.g. "1001") or GID.
    Used by ORDER_TRACKING, ORDER_STATUS, and handle_order_detail.

    customer_id is REQUIRED for the order to be returned: the executor only
    releases order contents when the order belongs to that customer. Passing
    None (or an unresolved placeholder) yields an empty result by design —
    the Admin API can read any order in the store, so ownership must be
    proven rather than assumed.
    """
    body = {
        "_op":      "fetch_order",
        "order_id": str(order_id),
    }
    if customer_id is not None:
        body["customer_gid"] = _customer_gid(customer_id)

    return WooAPICall(
        method="GET",
        endpoint="orders",
        params={},
        body=body,
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
    
def build_top_selling_products_call(
    top_n: int = 5,
    window_days: int = 30,
    date_after: str = None,
    date_before: str = None,
    collection_slugs=None,
    group_by_collection: bool = False,
    max_groups: int = 6,
    description: str = "",
) -> WooAPICall:
    """
    Shop-wide "top selling products" over a date window.

    Unlike every other builder in this module this call is NOT customer-scoped:
    it ranks the whole store's sales. No customer_gid is written, which also
    keeps it clear of ShopifyOrdersExecutor's not-logged-in guard — that guard
    matches on the placeholder appearing in customer_gid, and an absent key
    never matches.
    """
    body = {
        "_op":         "top_selling_products",
        "top_n":       top_n,
        "window_days": window_days,
    }
    if date_after:
        body["date_after"] = date_after
    if date_before:
        body["date_before"] = date_before
    if collection_slugs:
        body["collection_slugs"] = list(collection_slugs)
    if group_by_collection:
        body["group_by_collection"] = True
        body["max_groups"] = max_groups

    return WooAPICall(
        method="GET",
        endpoint="orders",
        params={},
        body=body,
        description=description or f"Shopify: top {top_n} products by units sold",
        surface="shopify_orders",
    )