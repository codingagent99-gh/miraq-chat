"""
Builds WooCommerce API calls using your actual store structure.
Includes CATEGORY-based and ORDER HISTORY/REORDER API calls.
"""

from typing import List
from models import Intent, ClassifiedResult, WooAPICall, ExtractedEntities
from store_registry import TAGS, ATTRIBUTES, get_store_loader


BASE = "https://wgc.net.in/hn/wp-json/wc/v3"


def build_api_calls(result: ClassifiedResult) -> List[WooAPICall]:
    """Build one or more WooCommerce API calls from classified result."""
    intent = result.intent
    e = result.entities
    calls = []

    # ═══════════════════════════════════════════
    # ★ ORDER HISTORY / REORDER / ORDER ITEM (NEW)
    # ═══════════════════════════════════════════

    if intent == Intent.LAST_ORDER:
        # GET /orders — fetch the most recent order for this customer
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={
                "customer": "CURRENT_USER_ID",
                "per_page": 1,
                "orderby": "date",
                "order": "desc",
            },
            description="Get the customer's most recent order",
            requires_resolution=["customer_id"],
        ))

    elif intent == Intent.ORDER_HISTORY:
        # GET /orders — fetch recent orders for this customer
        count = e.order_count or 10
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={
                "customer": "CURRENT_USER_ID",
                "per_page": count,
                "orderby": "date",
                "order": "desc",
            },
            description=f"Get customer's last {count} orders",
            requires_resolution=["customer_id"],
        ))

    elif intent == Intent.REORDER:
        # Step 1: Fetch the last order to get its line_items
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={
                "customer": "CURRENT_USER_ID",
                "per_page": 1,
                "orderby": "date",
                "order": "desc",
            },
            description="Fetch last order for reorder (step 1: get line items)",
            requires_resolution=["customer_id", "reorder_step2"],
        ))
        # Step 2 (POST to create order) will be built dynamically
        # in the server after we resolve the line_items from step 1

    elif intent == Intent.ORDER_ITEM:
        # Search for the product by name, then create an order
        product_name = e.order_item_name or e.product_name or ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "search": product_name,
                "status": "publish",
                "per_page": 5,
            },
            description=f"Find product '{product_name}' for ordering",
            requires_resolution=["order_item_step2"],
        ))

    # ═══════════════════════════════════════════
    # ★ CATEGORY-BASED BROWSING
    # ═══════════════════════════════════════════

    elif intent == Intent.CATEGORY_BROWSE:
        params = {
            "per_page": 20,
            "status": "publish",
            "category": str(e.category_id),
        }
        if e.on_sale:
            params["on_sale"] = "true"
        if e.color_tone:
            tag_id = _get_tag_id(e.tag_slugs)
            if tag_id:
                params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=(
                f"Browse category '{e.category_name}' "
                f"(id={e.category_id}, slug={e.category_slug})"
            ),
        ))

    elif intent == Intent.CATEGORY_LIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/categories",
            params={
                "per_page": 100,
                "hide_empty": True,
                "orderby": "name",
                "order": "asc",
            },
            description="List all product categories",
        ))

    # ═══════════════════════════════════════════
    # PRODUCT DISCOVERY
    # ═══════════════════════════════════════════

    elif intent == Intent.PRODUCT_LIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "per_page": 20,
                "status": "publish",
                "stock_status": "instock",
                "orderby": "menu_order",
                "order": "asc",
            },
            description="List all published, in-stock products",
        ))

    elif intent == Intent.PRODUCT_SEARCH:
        params = {
            "per_page": 20,
            "status": "publish",
            "search": e.product_name or e.search_term or "",
        }
        if e.product_slug:
            params["sku"] = f"{e.product_slug}-series"
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Search products matching '{e.product_name}'",
        ))

    elif intent == Intent.PRODUCT_DETAIL:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "search": e.product_name,
                "status": "publish",
                "per_page": 5,
            },
            description=f"Get details for '{e.product_name}' series",
        ))

    elif intent == Intent.PRODUCT_CATALOG:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/categories",
            params={"per_page": 100, "hide_empty": True},
            description="Get all product categories",
        ))
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/tags",
            params={"per_page": 100, "hide_empty": True},
            description="Get all product tags",
        ))

    elif intent == Intent.PRODUCT_TYPES:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/attributes/{ATTRIBUTES['pa_visual']['id']}/terms",
            params={"per_page": 100},
            description="List all visual/type options",
        ))

    elif intent == Intent.PRODUCT_BY_COLLECTION:
        tag_id = _get_tag_id(e.tag_slugs)
        params = {"per_page": 20, "status": "publish", "stock_status": "instock"}
        if tag_id:
            params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Products from {e.collection_year} collection",
        ))

    elif intent == Intent.PRODUCT_BY_ORIGIN:
        tag_id = _get_tag_id(e.tag_slugs)
        params = {"per_page": 20, "status": "publish"}
        if tag_id:
            params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Products from {e.origin}",
        ))

    elif intent == Intent.PRODUCT_QUICK_SHIP:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "per_page": 20,
                "status": "publish",
                "stock_status": "instock",
                "tag": str(TAGS["quick-ship"]["id"]),
            },
            description="Quick ship / in-stock products",
        ))

    elif intent == Intent.PRODUCT_BY_VISUAL:
        tag_id = _get_tag_id(e.tag_slugs)
        params = {"per_page": 20, "status": "publish"}
        if tag_id:
            params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Products with '{e.visual}' visual/look",
        ))

    elif intent == Intent.RELATED_PRODUCTS:
        if e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": e.product_name, "per_page": 1, "status": "publish"},
                description=f"Find '{e.product_name}' to get related_ids",
            ))

    # ═══════════════════════════════════════════
    # ATTRIBUTE FILTERS
    # ═══════════════════════════════════════════

    elif intent == Intent.FILTER_BY_FINISH:
        tag_id = _get_tag_id(e.tag_slugs)
        params = {"per_page": 20, "status": "publish"}
        if tag_id:
            params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Filter by finish: {e.finish}",
        ))

    elif intent == Intent.FILTER_BY_SIZE:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "per_page": 100,
                "status": "publish",
                "search": e.tile_size.replace('"', '') if e.tile_size else "",
            },
            description=f"Filter by tile size: {e.tile_size}",
        ))

    elif intent == Intent.FILTER_BY_COLOR:
        tag_id = _get_tag_id(e.tag_slugs)
        params = {"per_page": 20, "status": "publish"}
        if tag_id:
            params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Filter by color: {e.color_tone}",
        ))

    elif intent == Intent.FILTER_BY_THICKNESS:
        tag_id = _get_tag_id(e.tag_slugs)
        params = {"per_page": 20, "status": "publish"}
        if tag_id:
            params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Filter by thickness: {e.thickness}",
        ))

    elif intent == Intent.FILTER_BY_ORIGIN:
        tag_id = _get_tag_id(e.tag_slugs)
        params = {"per_page": 20, "status": "publish"}
        if tag_id:
            params["tag"] = str(tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Filter by origin: {e.origin}",
        ))

    elif intent == Intent.FILTER_BY_APPLICATION:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "per_page": 100,
                "status": "publish",
                "search": e.application or "",
            },
            description=f"Filter by application: {e.application}",
        ))

    elif intent == Intent.SIZE_LIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/attributes/{ATTRIBUTES['pa_tile-size']['id']}/terms",
            params={"per_page": 100},
            description="List all available tile sizes",
        ))

    # ═══════════════════════════════════════════
    # PRODUCT SUBTYPES
    # ═══════════════════════════════════════════

    elif intent == Intent.MOSAIC_PRODUCTS:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "status": "publish", "search": "mosaic"},
            description="List all mosaic products",
        ))

    elif intent == Intent.TRIM_PRODUCTS:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "status": "publish", "search": "bullnose"},
            description="List all trim/bullnose products",
        ))

    elif intent == Intent.CHIP_CARD:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={
                "per_page": 50,
                "status": "publish",
                "tag": str(TAGS["chip-card"]["id"]),
            },
            description="List all chip card products",
        ))

    # ═══════════════════════════════════════════
    # VARIATIONS
    # ═══════════════════════════════════════════

    elif intent == Intent.PRODUCT_VARIATIONS:
        if e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={
                    "search": e.product_name,
                    "status": "publish",
                    "type": "variable",
                    "per_page": 5,
                },
                description=f"Find variable product '{e.product_name}'",
            ))

    elif intent == Intent.SAMPLE_REQUEST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/attributes/{ATTRIBUTES['pa_sample-size']['id']}/terms",
            params={"per_page": 100},
            description="List available sample sizes",
        ))

    # ═══════════════════════════════════════════
    # DISCOUNTS & PROMOTIONS
    # ═══════════════════════════════════════════

    elif intent == Intent.DISCOUNT_INQUIRY:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"on_sale": "true", "per_page": 20, "status": "publish"},
            description="List products on sale",
        ))

    elif intent == Intent.CLEARANCE_PRODUCTS:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"on_sale": "true", "per_page": 20, "status": "publish"},
            description="List clearance products",
        ))

    elif intent == Intent.BULK_DISCOUNT:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "status": "publish", "search": "bulk"},
            description="Check for bulk discount products",
        ))

    elif intent == Intent.PROMOTIONS:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"on_sale": "true", "per_page": 20, "status": "publish"},
            description="List current promotions",
        ))

    elif intent == Intent.COUPON_INQUIRY:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/coupons",
            params={"per_page": 20},
            description="List available coupon codes",
        ))

    # ═══════════════════════════════════════════
    # ACCOUNT & ORDERING (existing)
    # ═══════════════════════════════════════════

    elif intent == Intent.SAVE_FOR_LATER:
        calls.append(WooAPICall(
            method="POST",
            endpoint=f"{BASE}/wishlist/add",
            params={},
            body={"product_id": e.product_id},
            description="Save item for later",
        ))

    elif intent == Intent.WISHLIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/wishlist",
            params={"customer_id": "CURRENT_USER"},
            description="Get customer wishlist",
        ))

    elif intent in (Intent.ORDER_TRACKING, Intent.ORDER_STATUS):
        if e.order_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders/{e.order_id}",
                params={},
                description=f"Get order #{e.order_id} details",
            ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders",
                params={
                    "customer": "CURRENT_USER_ID",
                    "per_page": 5,
                    "orderby": "date",
                    "order": "desc",
                },
                description="List recent orders (no order ID provided)",
            ))

    elif intent == Intent.PLACE_ORDER:
        line_items = []
        if e.product_id:
            item = {"product_id": e.product_id, "quantity": e.quantity or 1}
            if e.variation_id:
                item["variation_id"] = e.variation_id
            line_items.append(item)
        calls.append(WooAPICall(
            method="POST",
            endpoint=f"{BASE}/orders",
            params={},
            body={
                "status": "pending",
                "customer_id": "CURRENT_USER_ID",
                "line_items": line_items,
            },
            description="Create new order",
        ))

    # ═══════════════════════════════════════════
    # FALLBACK
    # ═══════════════════════════════════════════

    if not calls:
        search = (
            e.product_name or e.search_term or e.visual
            or e.finish or e.color_tone or e.origin
            or e.tile_size or e.thickness or "tiles"
        )
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"search": search, "per_page": 20, "status": "publish"},
            description=f"Fallback search: '{search}'",
        ))

    result.api_calls = calls
    return calls


def _get_tag_id(tag_slugs: list):
    """Get the first resolved tag ID from slug list."""
    for slug in tag_slugs:
        if slug in TAGS:
            return TAGS[slug]["id"]
    return None