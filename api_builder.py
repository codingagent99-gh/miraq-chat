"""
Builds WooCommerce API calls using live StoreLoader data.
No hardcoded tag/attribute IDs — everything resolved through StoreLoader.
"""

import json
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


def _category_slug(category_id: int) -> Optional[str]:
    """Get category slug by ID from live data."""
    l = _loader()
    return l.get_category_slug(category_id) if l else None


def _build_advanced_filter_call(
    tags: List[str] = None,
    categories: List[str] = None,
    attributes: dict = None,
    page: int = 1,
    per_page: int = 20,
    description: str = "",
) -> WooAPICall:
    """
    Build a single WooAPICall for the unified products-advanced endpoint.
    """
    filters = []

    if tags:
        filters.append({"tag": ",".join(tags)})

    if categories:
        filters.append({"category": ",".join(categories)})

    if attributes:
        for attr_taxonomy, terms_str in attributes.items():
            filters.append({"attribute": attr_taxonomy, "terms": terms_str})

    return WooAPICall(
        method="GET",
        endpoint=f"{CUSTOM_API_BASE}/products-advanced",
        params={
            "filters": json.dumps(filters),
            "page": page,
            "per_page": per_page,
        },
        description=description or "Advanced product filter",
        is_custom_api=True,
    )


def build_api_calls(result: ClassifiedResult, page: int = 1) -> List[WooAPICall]:
    """Build one or more WooCommerce API calls from classified result."""
    intent = result.intent
    e = result.entities
    calls = []

    # ═══════════════════════════════════════════
    # GREETING - No API calls needed
    # ═══════════════════════════════════════════
    
    if intent == Intent.GREETING:
        # Greetings don't require any WooCommerce API calls
        result.api_calls = []
        return []

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
            params={"customer": "CURRENT_USER_ID", "per_page": count, "page": page, "orderby": "date", "order": "desc"},
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
        cat_slug = _category_slug(e.category_id) if e.category_id else None
        categories_list = [cat_slug] if cat_slug else []

        # Collect tag slugs
        tag_slugs = []
        if e.tag_slugs:
            tag_slugs = list(e.tag_slugs)

        # Collect attribute filters
        attr_filters = {}
        _ATTR_TO_ENTITY = {
            "pa_tile-size": lambda ent: (ent.tile_size or "").replace('"', ''),
            "pa_finish": lambda ent: ent.finish or "",
            "pa_colors": lambda ent: ent.color_tone or "",
            "pa_thickness": lambda ent: ent.thickness or "",
            "pa_edge": lambda ent: ent.edge or "",
            "pa_application": lambda ent: ent.application or "",
            "pa_visual": lambda ent: ent.visual or "",
            "pa_origin": lambda ent: ent.origin or "",
        }
        if e.attribute_slug:
            resolver = _ATTR_TO_ENTITY.get(e.attribute_slug)
            term_value = resolver(e) if resolver else ""
            if term_value:
                attr_filters[e.attribute_slug] = term_value

        calls.append(_build_advanced_filter_call(
            tags=tag_slugs if tag_slugs else None,
            categories=categories_list if categories_list else None,
            attributes=attr_filters if attr_filters else None,
            page=page,
            description=f"Browse category '{e.category_name}' (id={e.category_id})",
        ))

    elif intent == Intent.CATEGORY_LIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/categories",
            params={"per_page": 100, "page": page, "hide_empty": True, "orderby": "name", "order": "asc"},
            description="List all product categories",
        ))

    # ═══════════════════════════════════════════
    # PRODUCT DISCOVERY
    # ═══════════════════════════════════════════

    elif intent == Intent.PRODUCT_LIST:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "page": page, "status": "publish", "stock_status": "instock",
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
                    params={"per_page": 100, "page": page, "status": "publish"},
                    description=f"Fetch variations for id={e.product_id}",
                ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"per_page": 20, "page": page, "status": "publish", "search": e.product_name or e.search_term or ""},
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
        if e.tag_slugs:
            calls.append(_build_advanced_filter_call(
                tags=list(e.tag_slugs),
                page=page,
                description=f"Products from {e.collection_year} collection (tags: {','.join(e.tag_slugs)})",
            ))
        else:
            # Fallback to standard API with tag IDs (keep existing behavior for when no slugs)
            params = {"per_page": 20, "page": page, "status": "publish", "stock_status": "instock"}
            if e.tag_ids:
                params["tag"] = str(e.tag_ids[0])
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params=params,
                description=f"Products from {e.collection_year} collection",
            ))

    elif intent == Intent.PRODUCT_BY_TAG:
        if e.tag_slugs:
            calls.append(_build_advanced_filter_call(
                tags=list(e.tag_slugs),
                page=page,
                description=f"Products by tag (slugs: {','.join(e.tag_slugs)})",
            ))
        else:
            params = {"per_page": 20, "page": page, "status": "publish", "stock_status": "instock"}
            if e.tag_ids:
                params["tag"] = str(e.tag_ids[0])
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params=params,
                description=f"Products by tag (id: {e.tag_ids[0] if e.tag_ids else 'unknown'})",
            ))

    elif intent == Intent.PRODUCT_BY_ORIGIN:
        calls.append(_build_advanced_filter_call(
            attributes={"pa_origin": e.origin or ""},
            page=page,
            description=f"Products from {e.origin}",
        ))

    elif intent == Intent.PRODUCT_QUICK_SHIP:
        params = {"per_page": 20, "page": page, "status": "publish", "stock_status": "instock"}
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
        calls.append(_build_advanced_filter_call(
            attributes={"pa_visual": e.visual or ""},
            page=page,
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

    elif intent == Intent.PRODUCT_CATALOG:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/categories",
            params={"per_page": 100, "page": page, "hide_empty": True},
            description="Get all product categories",
        ))
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products/tags",
            params={"per_page": 100, "page": page, "hide_empty": True},
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
        calls.append(_build_advanced_filter_call(
            attributes={"pa_finish": e.finish or ""},
            page=page,
            description=f"Filter by finish: {e.finish}",
        ))

    elif intent == Intent.FILTER_BY_SIZE:
        size_term = e.tile_size.replace('"', '') if e.tile_size else ""
        calls.append(_build_advanced_filter_call(
            attributes={"pa_tile-size": size_term},
            page=page,
            description=f"Filter by tile size: {e.tile_size}",
        ))

    elif intent == Intent.FILTER_BY_COLOR:
        calls.append(_build_advanced_filter_call(
            attributes={"pa_colors": e.color_tone or ""},
            page=page,
            description=f"Filter by color: {e.color_tone}",
        ))

    elif intent == Intent.FILTER_BY_THICKNESS:
        calls.append(_build_advanced_filter_call(
            attributes={"pa_thickness": e.thickness or ""},
            page=page,
            description=f"Filter by thickness: {e.thickness}",
        ))

    elif intent == Intent.FILTER_BY_EDGE:
        calls.append(_build_advanced_filter_call(
            attributes={"pa_edge": e.edge or ""},
            page=page,
            description=f"Filter by edge: {e.edge}",
        ))

    elif intent == Intent.FILTER_BY_APPLICATION:
        calls.append(_build_advanced_filter_call(
            attributes={"pa_application": e.application or ""},
            page=page,
            description=f"Filter by application: {e.application}",
        ))

    elif intent == Intent.FILTER_BY_MATERIAL:
        calls.append(_build_advanced_filter_call(
            attributes={"pa_visual": e.visual or ""},
            page=page,
            description=f"Filter by material: {e.visual}",
        ))

    elif intent == Intent.FILTER_BY_ORIGIN:
        calls.append(_build_advanced_filter_call(
            attributes={"pa_origin": e.origin or ""},
            page=page,
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
            params={"per_page": 20, "page": page, "status": "publish", "search": search_term},
            description=f"Search mosaic products: '{search_term}'",
        ))

    elif intent == Intent.TRIM_PRODUCTS:
        search_term = f"{e.product_name} bullnose" if e.product_name else "bullnose"
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "page": page, "status": "publish", "search": search_term},
            description=f"List trim products",
        ))

    elif intent == Intent.CHIP_CARD:
        if e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"per_page": 10, "page": page, "status": "publish",
                        "search": f"{e.product_name} chip card"},
                description=f"Find chip card for '{e.product_name}'",
            ))
        else:
            cc_tag_id = _tag_id("chip-card")
            params = {"per_page": 50, "page": page, "status": "publish"}
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
                params={"per_page": 100, "page": page, "status": "publish"},
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
            params={"on_sale": "true", "per_page": 20, "page": page, "status": "publish"},
            description="List products on sale",
        ))

    elif intent == Intent.CLEARANCE_PRODUCTS:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"on_sale": "true", "per_page": 20, "page": page, "status": "publish"},
            description="List clearance products",
        ))

    elif intent == Intent.BULK_DISCOUNT:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": 20, "page": page, "status": "publish", "search": "bulk"},
            description="Check for bulk discount products",
        ))

    elif intent == Intent.PROMOTIONS:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"on_sale": "true", "per_page": 20, "page": page, "status": "publish"},
            description="List current promotions",
        ))

    elif intent == Intent.COUPON_INQUIRY:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/coupons",
            params={"per_page": 20, "page": page},
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
                params={"customer": "CURRENT_USER_ID", "per_page": 5, "page": page,
                        "orderby": "date", "order": "desc"},
                description="List recent orders (no order ID provided)",
            ))

    elif intent == Intent.PLACE_ORDER:
        # For PLACE_ORDER, we search for the product but don't create the order here
        # The chat endpoint Step 3.6 handles order creation with proper product resolution
        # This prevents duplicate order creation
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Fetch product id={e.product_id} for order placement",
            ))
        elif e.product_name or e.order_item_name:
            search_term = e.product_name or e.order_item_name
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": search_term, "status": "publish", "per_page": 5},
                description=f"Find product '{search_term}' for order placement",
            ))
        # Note: Order creation happens in Step 3.6 of the chat endpoint

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
            params={"search": search, "per_page": 20, "page": page, "status": "publish"},
            description=f"Fallback search: '{search}'",
        ))

    result.api_calls = calls
    return calls