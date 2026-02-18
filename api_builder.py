"""
Builds WooCommerce API calls using live StoreLoader data.
No hardcoded tag/attribute IDs — everything resolved through StoreLoader.
"""

from typing import List, Optional
from models import Intent, ClassifiedResult, WooAPICall, ExtractedEntities
from store_registry import get_store_loader


BASE = "https://wgc.net.in/hn/wp-json/wc/v3"
CUSTOM_API_BASE = "https://wgc.net.in/hn/wp-json/custom-api/v1"


def _loader():
    """Convenience accessor for StoreLoader."""
    return get_store_loader()


def _tag_id(slug: str) -> Optional[int]:
    """Get tag ID by slug from live data."""
    l = _loader()
    return l.get_tag_id_by_slug(slug) if l else None


def _attr_id(slug: str) -> Optional[int]:
    """Get attribute ID by slug from live data."""
    l = _loader()
    return l.get_attribute_id(slug) if l else None


def _first_tag_id(tag_ids: list) -> Optional[int]:
    """Return first tag ID from a list, or None."""
    return tag_ids[0] if tag_ids else None


def _attr_filter_params(attr_slug: str, term_ids: List[int], tag_ids: List[int]) -> dict:
    """
    Build the right WooCommerce filter params for an attribute.
    Uses attribute+attribute_term when term IDs are resolved (accurate).
    Falls back to tag= when only tag IDs are available.
    """
    params = {}
    if term_ids:
        attr_id = _attr_id(attr_slug)
        if attr_id:
            params["attribute"] = attr_slug
            params["attribute_term"] = ",".join(str(i) for i in term_ids)
    elif tag_ids:
        params["tag"] = str(_first_tag_id(tag_ids))
    return params


def build_api_calls(result: ClassifiedResult) -> List[WooAPICall]:
    """Build one or more WooCommerce API calls from classified result."""
    intent = result.intent
    e = result.entities
    calls = []

    # ═══════════════════════════════════════════
    # ORDER HISTORY / REORDER / ORDER ITEM
    # ═══════════════════════════════════════════

    if intent == Intent.LAST_ORDER:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={"customer": "CURRENT_USER_ID", "per_page": 1, "orderby": "date", "order": "desc"},
            description="Get the customer's most recent order",
            requires_resolution=["customer_id"],
        ))

    elif intent == Intent.ORDER_HISTORY:
        count = e.order_count or 10
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={"customer": "CURRENT_USER_ID", "per_page": count, "orderby": "date", "order": "desc"},
            description=f"Get customer's last {count} orders",
            requires_resolution=["customer_id"],
        ))

    elif intent == Intent.REORDER:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params={"customer": "CURRENT_USER_ID", "per_page": 1, "orderby": "date", "order": "desc"},
            description="Fetch last order for reorder (step 1)",
            requires_resolution=["customer_id", "reorder_step2"],
        ))

    elif intent == Intent.ORDER_ITEM:
        product_name = e.order_item_name or e.product_name or ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"search": product_name, "status": "publish", "per_page": 5},
            description=f"Find product '{product_name}' for ordering",
            requires_resolution=["order_item_step2"],
        ))

    elif intent == Intent.QUICK_ORDER:
        search_term = e.order_item_name or e.product_name or ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"search": search_term, "status": "publish", "per_page": 5},
            description=f"Find product '{search_term}' for quick order",
            requires_resolution=["create_order_from_product"],
        ))

    # ═══════════════════════════════════════════
    # CATEGORY-BASED BROWSING
    # ═══════════════════════════════════════════

    elif intent == Intent.CATEGORY_BROWSE:
        params = {"per_page": 20, "status": "publish", "category": str(e.category_id)}
        if e.on_sale:
            params["on_sale"] = "true"
        if e.tag_ids:
            params["tag"] = str(e.tag_ids[0])
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Browse category '{e.category_name}' (id={e.category_id})",
        ))

    elif intent == Intent.CATEGORY_LIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/categories",
            params={"per_page": 100, "hide_empty": True, "orderby": "name", "order": "asc"},
            description="List all product categories",
        ))

    # ═══════════════════════════════════════════
    # PRODUCT DISCOVERY
    # ═══════════════════════════════════════════

    elif intent == Intent.PRODUCT_LIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "status": "publish", "stock_status": "instock",
                    "orderby": "menu_order", "order": "asc"},
            description="List all published, in-stock products",
        ))

    elif intent == Intent.PRODUCT_SEARCH:
        has_attributes = any([e.finish, e.color_tone, e.tile_size, e.thickness, e.visual, e.origin, e.application])
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Fetch product id={e.product_id} ('{e.product_name}')",
            ))
            if has_attributes:
                calls.append(WooAPICall(
                    method="GET",
                    endpoint=f"{BASE}/products/{e.product_id}/variations",
                    params={"per_page": 100, "status": "publish"},
                    description=f"Fetch variations for id={e.product_id}",
                ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"per_page": 20, "status": "publish", "search": e.product_name or e.search_term or ""},
                description=f"Search products matching '{e.product_name}'",
            ))

    elif intent == Intent.PRODUCT_DETAIL:
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Get details for product id={e.product_id}",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Get variations for '{e.product_name}'",
            ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": e.product_name, "status": "publish", "per_page": 5},
                description=f"Search product '{e.product_name}'",
            ))

    elif intent == Intent.PRODUCT_BY_COLLECTION:
        params = {"per_page": 20, "status": "publish", "stock_status": "instock"}
        if e.tag_ids:
            params["tag"] = str(e.tag_ids[0])
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Products from {e.collection_year} collection",
        ))

    elif intent == Intent.PRODUCT_BY_ORIGIN:
        # Use custom API for attribute filtering
        term_value = e.origin.lower() if e.origin else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_origin", "term": term_value},
            description=f"Products from {e.origin}",
            is_custom_api=True,
        ))

    elif intent == Intent.PRODUCT_QUICK_SHIP:
        params = {"per_page": 20, "status": "publish", "stock_status": "instock"}
        qs_tag_id = _tag_id("quick-ship")
        if qs_tag_id:
            params["tag"] = str(qs_tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description="Quick ship / in-stock products",
        ))

    elif intent == Intent.PRODUCT_BY_VISUAL:
        # Use custom API for attribute filtering
        term_value = e.visual.lower() if e.visual else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_visual", "term": term_value},
            description=f"Products with '{e.visual}' visual/look",
            is_custom_api=True,
        ))

    elif intent == Intent.RELATED_PRODUCTS:
        if e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": e.product_name, "per_page": 1, "status": "publish"},
                description=f"Find '{e.product_name}' to get related_ids",
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
        attr_id = _attr_id("pa_visual")
        if attr_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/attributes/{attr_id}/terms",
                params={"per_page": 100},
                description="List all visual/type options",
            ))

    # ═══════════════════════════════════════════
    # ATTRIBUTE FILTERS — use attribute+term when available
    # ═══════════════════════════════════════════

    elif intent == Intent.FILTER_BY_FINISH:
        # Use custom API for attribute filtering
        term_value = e.finish.lower() if e.finish else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_finish", "term": term_value},
            description=f"Filter by finish: {e.finish}",
            is_custom_api=True,
        ))

    elif intent == Intent.FILTER_BY_SIZE:
        # Use custom API for attribute filtering
        # Strip quotes from tile_size to get clean size like 12x12
        term_value = e.tile_size.replace('"', '') if e.tile_size else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_tile-size", "term": term_value},
            description=f"Filter by tile size: {e.tile_size}",
            is_custom_api=True,
        ))

    elif intent == Intent.FILTER_BY_COLOR:
        # Use custom API for attribute filtering
        term_value = e.color_tone.lower() if e.color_tone else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_colors", "term": term_value},
            description=f"Filter by color: {e.color_tone}",
            is_custom_api=True,
        ))

    elif intent == Intent.FILTER_BY_THICKNESS:
        # Use custom API for attribute filtering
        term_value = e.thickness if e.thickness else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_thickness", "term": term_value},
            description=f"Filter by thickness: {e.thickness}",
            is_custom_api=True,
        ))

    elif intent == Intent.FILTER_BY_APPLICATION:
        # Use custom API for attribute filtering
        term_value = e.application.lower() if e.application else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_application", "term": term_value},
            description=f"Filter by application: {e.application}",
            is_custom_api=True,
        ))

    elif intent == Intent.FILTER_BY_EDGE:
        # Use custom API for attribute filtering
        term_value = e.edge.lower() if e.edge else ""
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{CUSTOM_API_BASE}/products-by-attribute",
            params={"attribute": "pa_edge", "term": term_value},
            description=f"Filter by edge: {e.edge}",
            is_custom_api=True,
        ))

    elif intent == Intent.FILTER_BY_ORIGIN:
        params = {"per_page": 20, "status": "publish"}
        if e.tag_ids:
            params["tag"] = str(e.tag_ids[0])
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description=f"Filter by origin: {e.origin}",
        ))

    elif intent == Intent.SIZE_LIST:
        attr_id = _attr_id("pa_tile-size")
        if attr_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/attributes/{attr_id}/terms",
                params={"per_page": 100},
                description="List all available tile sizes",
            ))

    # ═══════════════════════════════════════════
    # PRODUCT SUBTYPES
    # ═══════════════════════════════════════════

    elif intent == Intent.MOSAIC_PRODUCTS:
        search_term = f"{e.product_name} mosaic" if e.product_name else "mosaic"
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "status": "publish", "search": search_term},
            description=f"Search mosaic products: '{search_term}'",
        ))

    elif intent == Intent.TRIM_PRODUCTS:
        search_term = f"{e.product_name} bullnose" if e.product_name else "bullnose"
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "status": "publish", "search": search_term},
            description=f"List trim products",
        ))

    elif intent == Intent.CHIP_CARD:
        if e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"per_page": 10, "status": "publish",
                        "search": f"{e.product_name} chip card"},
                description=f"Find chip card for '{e.product_name}'",
            ))
        else:
            cc_tag_id = _tag_id("chip-card")
            params = {"per_page": 50, "status": "publish"}
            if cc_tag_id:
                params["tag"] = str(cc_tag_id)
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params=params,
                description="List all chip card products",
            ))

    # ═══════════════════════════════════════════
    # VARIATIONS
    # ═══════════════════════════════════════════

    elif intent == Intent.PRODUCT_VARIATIONS:
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Get parent product '{e.product_name}'",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Get all variations for '{e.product_name}'",
            ))
        elif e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": e.product_name, "status": "publish",
                        "type": "variable", "per_page": 5},
                description=f"Find variable product '{e.product_name}'",
            ))

    elif intent == Intent.SAMPLE_REQUEST:
        attr_id = _attr_id("pa_sample-size")
        if attr_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/attributes/{attr_id}/terms",
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
    # ACCOUNT & ORDERING
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
                params={"customer": "CURRENT_USER_ID", "per_page": 5,
                        "orderby": "date", "order": "desc"},
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
                "status": "processing",
                "customer_id": "CURRENT_USER_ID",
                "payment_method": "cod",
                "payment_method_title": "Cash on Delivery",
                "set_paid": False,
                "line_items": line_items,
            },
            description="Create new order (COD)",
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