"""
WooCommerce API Parameter Builder
===================================
Converts classified intents + entities into WooCommerce REST API requests.

Base URL: https://yourstore.com/wp-json/wc/v3/
Auth:     Basic Auth with consumer_key + consumer_secret
"""

from intent_classifier import Intent, ClassifiedResult, ExtractedEntities
from dataclasses import dataclass
from typing import Optional


BASE_URL = "https://yourstore.com/wp-json/wc/v3"


@dataclass
class WooCommerceAPICall:
    method: str               # GET, POST, PUT
    endpoint: str             # Full URL
    params: dict              # Query parameters
    body: Optional[dict]      # JSON body (for POST/PUT)
    description: str          # Human-readable explanation


def build_api_call(result: ClassifiedResult) -> WooCommerceAPICall:
    """Build a WooCommerce API call from a classified intent."""

    intent = result.intent
    e = result.entities

    # ═══════════════════════════════════════════
    # PRODUCT DISCOVERY & SEARCH
    # ═══════════════════════════════════════════

    if intent == Intent.PRODUCT_LIST:
        # GET /products — list all products (optionally filtered)
        params = {"per_page": 20, "status": "publish"}
        if e.search_term:
            params["search"] = e.search_term
        if e.product_type:
            params["search"] = e.product_type if "search" not in params else params["search"]
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products",
            params=params,
            body=None,
            description="List all published tile products",
        )

    if intent == Intent.PRODUCT_CATEGORY_BROWSE:
        # Step 1: Resolve category slug → ID via GET /products/categories?slug=xxx
        # Step 2: GET /products?category=<id>
        params = {"per_page": 20, "status": "publish"}
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products",
            params={
                **params,
                "_category_slug": e.category,  # Resolve to ID at runtime (see resolver below)
            },
            body=None,
            description=f"Browse products in category '{e.category}'",
        )

    if intent == Intent.PRODUCT_CATALOG:
        # GET /products/categories — list all product categories
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products/categories",
            params={"per_page": 100, "hide_empty": True},
            body=None,
            description="Retrieve full product catalog (all categories)",
        )

    if intent == Intent.PRODUCT_TYPES:
        # GET /products/categories — show all tile types/categories
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products/categories",
            params={"per_page": 100, "hide_empty": True},
            body=None,
            description="List all tile types/categories available",
        )

    # ═══════════════════════════════════════════
    # SIZE & DIMENSIONS
    # ═══════════════════════════════════════════

    if intent == Intent.SIZE_LIST:
        # GET /products/attributes/<size_attr_id>/terms
        # Assumes 'Size' attribute exists; ID resolved at runtime
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products/attributes",
            params={"per_page": 100},
            body=None,
            description="List all product attributes (to find size attribute and its terms)",
        )

    if intent == Intent.SIZE_FILTER:
        # GET /products?tag=<tag_id> OR ?attribute=pa_size&attribute_term=<term_id>
        params = {"per_page": 20, "status": "publish"}
        if e.tag:
            params["_tag_slug"] = e.tag  # Resolve to ID at runtime
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products",
            params=params,
            body=None,
            description=f"Filter products by size: '{e.size}'",
        )

    # ═══════════════════════════════════════════
    # DISCOUNTS & PROMOTIONS
    # ═══════════════════════════════════════════

    if intent == Intent.DISCOUNT_INQUIRY:
        # GET /products?on_sale=true
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products",
            params={"on_sale": True, "per_page": 20, "status": "publish"},
            body=None,
            description="List all products currently on sale",
        )

    if intent == Intent.BULK_DISCOUNT:
        # No direct WooCommerce API — check meta or custom endpoint
        # Fallback: search products tagged 'bulk-discount'
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products",
            params={
                "per_page": 20,
                "status": "publish",
                "_tag_slug": "bulk-discount",  # Resolve at runtime
            },
            body=None,
            description="Check for bulk discount products/policies",
        )

    if intent == Intent.CLEARANCE_PRODUCTS:
        # GET /products?on_sale=true&tag=<clearance_tag_id>
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products",
            params={
                "on_sale": True,
                "per_page": 20,
                "status": "publish",
                "_tag_slug": "clearance",  # Resolve at runtime
            },
            body=None,
            description="List clearance/sale products",
        )

    if intent == Intent.PROMOTIONS:
        # GET /products?on_sale=true  +  GET /coupons
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/products",
            params={"on_sale": True, "per_page": 20, "status": "publish"},
            body=None,
            description="List all current promotions (on-sale products)",
        )

    if intent == Intent.COUPON_INQUIRY:
        # GET /coupons — list available coupons
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/coupons",
            params={"per_page": 20},
            body=None,
            description="List available coupon codes",
        )

    # ═══════════════════════════════════════════
    # ACCOUNT & ORDERING
    # ═══════════════════════════════════════════

    if intent == Intent.SAVE_FOR_LATER:
        # WooCommerce doesn't have a native "save for later" API.
        # Uses a custom endpoint or the YITH Wishlist plugin API.
        return WooCommerceAPICall(
            method="POST",
            endpoint=f"{BASE_URL}/wishlist/add",  # Custom / plugin endpoint
            params={},
            body={"product_id": e.product_id},
            description="Save item for later (requires Wishlist plugin, e.g., YITH)",
        )

    if intent == Intent.WISHLIST:
        # GET or POST to wishlist plugin endpoint
        return WooCommerceAPICall(
            method="GET",
            endpoint=f"{BASE_URL}/wishlist",  # Custom / plugin endpoint
            params={"customer_id": "current_user"},
            body=None,
            description="Get/create customer wishlist (requires Wishlist plugin)",
        )

    if intent == Intent.ORDER_TRACKING:
        # GET /orders/<order_id> — retrieve order with tracking info
        params = {}
        if e.order_id:
            return WooCommerceAPICall(
                method="GET",
                endpoint=f"{BASE_URL}/orders/{e.order_id}",
                params={},
                body=None,
                description=f"Track order #{e.order_id}",
            )
        else:
            # List recent orders for current customer
            return WooCommerceAPICall(
                method="GET",
                endpoint=f"{BASE_URL}/orders",
                params={"customer": "current_user_id", "per_page": 5, "orderby": "date", "order": "desc"},
                body=None,
                description="List recent orders for tracking (order ID needed)",
            )

    if intent == Intent.ORDER_STATUS:
        if e.order_id:
            return WooCommerceAPICall(
                method="GET",
                endpoint=f"{BASE_URL}/orders/{e.order_id}",
                params={},
                body=None,
                description=f"Get status of order #{e.order_id}",
            )
        else:
            return WooCommerceAPICall(
                method="GET",
                endpoint=f"{BASE_URL}/orders",
                params={"customer": "current_user_id", "per_page": 5, "orderby": "date", "order": "desc"},
                body=None,
                description="List recent orders (please provide order ID for status)",
            )

    if intent == Intent.PLACE_ORDER:
        # POST /orders — create a new order
        line_items = []
        if e.product_id:
            line_items.append({
                "product_id": e.product_id,
                "quantity": e.quantity or 1,
            })
        return WooCommerceAPICall(
            method="POST",
            endpoint=f"{BASE_URL}/orders",
            params={},
            body={
                "status": "pending",
                "line_items": line_items,
                "customer_id": "current_user_id",  # Resolve at runtime
                # billing & shipping populated from customer profile
            },
            description="Create a new order",
        )

    # ═════════════════════════════════════���═════
    # FALLBACK
    # ═══════════════════════════════════════════
    return WooCommerceAPICall(
        method="GET",
        endpoint=f"{BASE_URL}/products",
        params={"search": e.search_term or "", "per_page": 20},
        body=None,
        description="Fallback: general product search",
    )