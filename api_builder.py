"""
Builds WooCommerce API calls using live StoreLoader data.
No hardcoded tag/attribute IDs — everything resolved through StoreLoader.
No hardcoded store URLs or pa_* slugs — attribute taxonomies resolved
dynamically from loader.all_attributes_raw via _attr_slug_for_label().
"""

import json
import re
from typing import List, Optional
from models import Intent, ClassifiedResult, WooAPICall, ExtractedEntities
from store_registry import get_store_loader
from config.settings import DEFAULT_PER_PAGE, DEFAULT_ORDER_PER_PAGE
from app_config import WOO_BASE_URL, CUSTOM_API_BASE_URL
from config.store_config import (
    TAG_SLUG_QUICK_SHIP,
    FALLBACK_SEARCH_TERM,
)
import re

# Resolved at import time from env / app_config — no literals here.
BASE = WOO_BASE_URL
CUSTOM_API_BASE = CUSTOM_API_BASE_URL


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


def _attr_slug_for_label(label: str) -> Optional[str]:
    """
    Resolve a WooCommerce attribute taxonomy slug from an attribute label.
    e.g. "finish" → "pa_finish", "tile size" → "pa_tile-size"
    Uses live all_attributes_raw — no hardcoded ATTR_* constants needed.
    """
    l = _loader()
    if not l or not l.all_attributes_raw:
        return None
    label_lower = label.lower().strip()
    for attr in l.all_attributes_raw:
        if attr.get("attribute_label", "").lower().strip() == label_lower:
            return attr.get("taxonomy")
    return None


def _build_advanced_filter_call(
    tags: List[str] = None,
    categories: List[str] = None,
    attributes: dict = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
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


def match_variation_to_entities(variations: list, entities) -> Optional[dict]:
    """
    Given a list of WooCommerce variation dicts and extracted entities, return
    the variation that best matches the user's requested attributes.

    Scoring: +1 for each variation attribute whose name+option matches an
    extracted entity attribute value. The variation with the highest score wins.
    Ties broken by returning the first highest-scoring variation found.

    Returns the best-matching variation dict, or None if no match scored > 0.

    Usage (in chat.py or variant_handler.py after variations are fetched):
        from api_builder import match_variation_to_entities
        best = match_variation_to_entities(variations_list, entities)
        if best:
            price = best.get("price")

    Example:
        entities.attributes = {'finish': 'Silky', 'tile size': '3"x3"'}
        variation attrs      = [{'name': 'Finish', 'option': 'Silky'},
                                 {'name': 'Tile Size', 'option': '3"x3"'}]
        → score 2 → this variation wins
    """
    if not variations or not entities.attributes:
        return None

    best_variation = None
    best_score = 0

    for variation in variations:
        score = 0
        for attr in variation.get("attributes", []):
            attr_label = attr.get("name", "").lower().strip()
            attr_option = attr.get("option", "").lower().strip()
            for ent_label, ent_value in entities.attributes.items():
                if ent_label.lower().strip() == attr_label:
                    # Normalize both sides: strip quotes and extra spaces
                    ent_clean = re.sub(r'[\"\'`]', '', ent_value).strip().lower()
                    opt_clean = re.sub(r'[\"\'`]', '', attr_option).strip().lower()
                    if ent_clean == opt_clean or ent_clean in opt_clean or opt_clean in ent_clean:
                        score += 1
                        break
        if score > best_score:
            best_score = score
            best_variation = variation

    return best_variation if best_score > 0 else None


def build_api_calls(result: ClassifiedResult, page: int = 1) -> List[WooAPICall]:
    """Build one or more WooCommerce API calls from classified result."""
    intent = result.intent
    e = result.entities
    calls = []

    # ═══════════════════════════════════════════
    # GREETING - No API calls needed
    # ═══════════════════════════════════════════

    if intent == Intent.GREETING:
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
        count = e.order_count or DEFAULT_ORDER_PER_PAGE
        _order_params = {"customer": "CURRENT_USER_ID", "per_page": count, "page": page, "orderby": "date", "order": "desc"}
        if getattr(e, "date_after", None):
            _order_params["after"] = e.date_after
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params=_order_params,
            description=f"Get customer orders{' after ' + e.date_after[:10] if getattr(e, 'date_after', None) else ' (last ' + str(count) + ')'}",
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
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Fetch product id={e.product_id} ('{product_name}') for ordering",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Fetch variations for order resolution of '{product_name}'",
            ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": product_name, "status": "publish", "per_page": 5},
                description=f"Find product '{product_name}' for ordering",
                requires_resolution=["order_item_step2"],
            ))

    elif intent == Intent.QUICK_ORDER:
        search_term = e.order_item_name or e.product_name or ""
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Fetch product id={e.product_id} ('{search_term}') for quick order",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Fetch variations for quick order resolution of '{search_term}'",
            ))
        else:
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
        loader = get_store_loader()
        cat_id = e.category_id

        if not cat_id and e.category_name and loader:
            cat_id = loader.get_category_id(e.category_name)
            if cat_id:
                e.category_id = cat_id

        if cat_id and loader:
            categories_list = loader.get_all_slugs_for_category(cat_id)
        elif cat_id:
            cat_slug = _category_slug(cat_id)
            categories_list = [cat_slug] if cat_slug else []
        else:
            categories_list = []

        tag_slugs = list(e.tag_slugs) if e.tag_slugs else []

        # Build attribute filters from the dynamic entities.attributes dict.
        # e.attribute_slug is set by the classifier to the matched taxonomy.
        attr_filters = {}
        if e.attribute_slug and e.attributes:
            # Find which label maps to this taxonomy slug, then get its value
            l = get_store_loader()
            if l and l.all_attributes_raw:
                for attr in l.all_attributes_raw:
                    if attr.get("taxonomy") == e.attribute_slug:
                        label = attr.get("attribute_label", "").lower().strip()
                        term_value = e.attributes.get(label, "")
                        if term_value:
                            attr_filters[e.attribute_slug] = term_value
                        break

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
            params={"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish", "stock_status": "instock",
                    "orderby": "menu_order", "order": "asc"},
            description="List all published, in-stock products",
        ))

    elif intent == Intent.PRODUCT_SEARCH:
        has_attributes = bool(e.attributes)

        # ── Category-scoped product search ──────────────────────────────────
        # When BOTH a product name AND a category are present, the user wants
        # to browse that series *within* a category (e.g. "Titan Marbles in
        # Countertop"). Fetching the specific product's variations would return
        # ALL variants regardless of category; instead use the advanced filter
        # endpoint with a search term + category scope so only matching products
        # are returned with proper pagination.
        if e.product_name and e.category_id:
            loader = get_store_loader()
            cat_id = e.category_id
            if not cat_id and e.category_name and loader:
                cat_id = loader.get_category_id(e.category_name)
            categories_list = loader.get_all_slugs_for_category(cat_id) if (cat_id and loader) else []
            tag_slugs = list(e.tag_slugs) if e.tag_slugs else []
            # Build attribute filters if any attributes were also specified
            attr_filters = {}
            if e.attribute_slug and e.attributes and loader and loader.all_attributes_raw:
                for attr in loader.all_attributes_raw:
                    if attr.get("taxonomy") == e.attribute_slug:
                        label = attr.get("attribute_label", "").lower().strip()
                        term_value = e.attributes.get(label, "")
                        if term_value:
                            attr_filters[e.attribute_slug] = term_value
                        break
            # Use the product name as a search term inside the advanced filter
            call = _build_advanced_filter_call(
                tags=tag_slugs if tag_slugs else None,
                categories=categories_list if categories_list else None,
                attributes=attr_filters if attr_filters else None,
                page=page,
                description=f"Category-scoped search: '{e.product_name}' in '{e.category_name}'",
            )
            # Only inject a free-text search term when there are no tag slugs.
            # When tag_slugs are present they already scope the results precisely
            # (e.g. "titan-marbles-series" tag + "countertop" category is exact);
            # adding search="Titan Marbles" on top would further restrict and may
            # miss products whose title doesn't literally contain those words.
            if not tag_slugs:
                call.params["search"] = e.product_name
            calls.append(call)

        elif e.product_id:
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
        elif has_attributes and e.attribute_slug and not e.product_name:
            # Attribute match without a product name — use the advanced filter
            # endpoint so the attribute is applied. Dynamically resolves the
            # term value from e.attribute_slug, no hardcoded label names.
            attr_filters = {}
            l = get_store_loader()
            if l and l.all_attributes_raw:
                for attr in l.all_attributes_raw:
                    if attr.get("taxonomy") == e.attribute_slug:
                        label = attr.get("attribute_label", "").lower().strip()
                        term_value = e.attributes.get(label, "")
                        if term_value:
                            attr_filters[e.attribute_slug] = term_value
                        break
            cat_id = e.category_id
            if not cat_id and e.category_name and l:
                cat_id = l.get_category_id(e.category_name)
            categories_list = l.get_all_slugs_for_category(cat_id) if (cat_id and l) else []
            calls.append(_build_advanced_filter_call(
                categories=categories_list if categories_list else None,
                attributes=attr_filters if attr_filters else None,
                page=page,
                description=f"Attribute-scoped search: {e.attributes}",
            ))
        else:
            # If tag_slugs are set (e.g. resolved by LLM fallback from origin/demonym),
            # route through the advanced filter endpoint so the tag is actually applied.
            if e.tag_slugs:
                calls.append(_build_advanced_filter_call(
                    tags=list(e.tag_slugs),
                    page=page,
                    description=f"Tag-scoped product search (slugs: {', '.join(e.tag_slugs)})",
                ))
            else:
                calls.append(WooAPICall(
                    method="GET",
                    endpoint=f"{BASE}/products",
                    params={"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish",
                            "search": e.product_name or e.search_term or ""},
                    description=f"Search products matching '{e.product_name or e.search_term}'",
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

    elif intent == Intent.PRODUCT_ATTRIBUTE_INFO:
        # Fetch the product and its variations so the response generator can
        # read attributes[].options from the parent and cross-reference in-stock
        # variants. Same two calls as PRODUCT_DETAIL.
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Fetch product '{e.product_name}' for attribute info",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Fetch variations for '{e.product_name}' attribute info",
            ))
        elif e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": e.product_name, "status": "publish", "per_page": 5},
                description=f"Search product '{e.product_name}' for attribute info",
            ))

    elif intent == Intent.PRODUCT_BY_COLLECTION:
        if e.tag_slugs:
            calls.append(_build_advanced_filter_call(
                tags=list(e.tag_slugs),
                page=page,
                description=f"Products from {e.collection_year} collection (tags: {','.join(e.tag_slugs)})",
            ))
        else:
            params = {"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish", "stock_status": "instock"}
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
            params = {"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish", "stock_status": "instock"}
            if e.tag_ids:
                params["tag"] = str(e.tag_ids[0])
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params=params,
                description=f"Products by tag (id: {e.tag_ids[0] if e.tag_ids else 'unknown'})",
            ))

    elif intent == Intent.PRODUCT_BY_ORIGIN:
        origin = e.attributes.get("origin", "")
        calls.append(_build_advanced_filter_call(
            attributes={_attr_slug_for_label("origin"): origin} if _attr_slug_for_label("origin") else None,
            tags=list(e.tag_slugs) if e.tag_slugs else None,
            page=page,
            description=f"Products from {origin}",
        ))

    elif intent == Intent.PRODUCT_QUICK_SHIP:
        params = {"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish", "stock_status": "instock"}
        qs_tag_id = _tag_id(TAG_SLUG_QUICK_SHIP)
        if qs_tag_id:
            params["tag"] = str(qs_tag_id)
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params=params,
            description="Quick ship / in-stock products",
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
        # Dynamically find any "visual" or "type" attribute — no hardcoded label needed.
        l = _loader()
        if l and l.all_attributes_raw:
            for attr in l.all_attributes_raw:
                label = attr.get("attribute_label", "").lower()
                if "visual" in label or "type" in label:
                    type_slug = attr.get("taxonomy")
                    attr_id = _attr_id(type_slug) if type_slug else None
                    if attr_id:
                        calls.append(WooAPICall(
                            method="GET",
                            endpoint=f"{BASE}/products/attributes/{attr_id}/terms",
                            params={"per_page": 100},
                            description="List all product types/visuals",
                        ))
                    break

    # ═══════════════════════════════════════════
    # ATTRIBUTE FILTERS
    # ═══════════════════════════════════════════
        
    elif intent == Intent.FILTER_BY_ATTRIBUTE:
        attr_filters = {}
        for label, value in e.attributes.items():
            slug = _attr_slug_for_label(label)
            if slug and value:
                attr_filters[slug] = value

        cat_id = e.category_id
        loader = get_store_loader()
        if not cat_id and e.category_name and loader:
            cat_id = loader.get_category_id(e.category_name)
        categories_list = loader.get_all_slugs_for_category(cat_id) if (cat_id and loader) else []

        # Deduplicate: drop any tag whose slug tokens are fully covered by
        # already-resolved attribute values. This prevents double-filtering when
        # e.g. tag "1-4-thick" and attribute pa_thickness "1/4" thick" describe
        # the same concept. Works dynamically — no hardcoded slug/label names.
        attr_value_tokens = set()
        for v in e.attributes.values():
            attr_value_tokens |= {
                t for t in re.split(r'[\s\-_"/]+', v.lower()) if len(t) >= 2
            }
        deduped_tag_slugs = [
            slug for slug in (e.tag_slugs or [])
            if not {t for t in slug.split("-") if len(t) >= 2} <= attr_value_tokens
        ]

        attr_label = next(iter(e.attributes.keys()), "attribute")
        attr_value = next(iter(e.attributes.values()), "")
        calls.append(_build_advanced_filter_call(
            categories=categories_list if categories_list else None,
            attributes=attr_filters if attr_filters else None,
            tags=deduped_tag_slugs if deduped_tag_slugs else None,
            page=page,
            description=f"Filter by {attr_label}: {attr_value}",
        ))
        
    elif intent == Intent.FILTER_BY_ORIGIN:
        # Kept separate: origin uses tag-based resolution (demonym synonyms),
        # not just attribute term IDs, so needs both attribute and tag params.
        origin = e.attributes.get("origin", "")
        calls.append(_build_advanced_filter_call(
            attributes={e.attribute_slug: origin} if (e.attribute_slug and origin) else None,
            tags=list(e.tag_slugs) if e.tag_slugs else None,
            page=page,
            description=f"Filter by origin: {origin}",
        ))

    elif intent == Intent.SIZE_LIST:
        if e.product_id:
            # Product-scoped: fetch this product's variations to extract its actual sizes.
            # Much more useful than listing all global size terms in the store.
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Get parent product '{e.product_name}' for size list",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Get variations to extract available sizes for '{e.product_name}'",
            ))
        else:
            # No specific product — fall back to global size terms
            l = _loader()
            if l and l.all_attributes_raw:
                for attr in l.all_attributes_raw:
                    if "size" in attr.get("attribute_label", "").lower():
                        size_slug = attr.get("taxonomy")
                        attr_id = _attr_id(size_slug) if size_slug else None
                        if attr_id:
                            calls.append(WooAPICall(
                                method="GET",
                                endpoint=f"{BASE}/products/attributes/{attr_id}/terms",
                                params={"per_page": 100},
                                description="List all available sizes",
                            ))
                        break

    # ═══════════════════════════════════════════
    # PRODUCT SUBTYPES
    # ═══════════════════════════════════════════

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
        sample_slug = _attr_slug_for_label("sample size")
        attr_id = _attr_id(sample_slug) if sample_slug else None
        if attr_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/attributes/{attr_id}/terms",
                params={"per_page": 100},
                description="List available sample sizes",
            ))

    # ═══════════════════════════════════════════
    # DISCOUNTS & SALES
    # ═══════════════════════════════════════════

    elif intent == Intent.DISCOUNT_INQUIRY:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"on_sale": "true", "per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish"},
            description="List products on sale",
        ))

    elif intent == Intent.BULK_DISCOUNT:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish", "search": "bulk"},
            description="Check for bulk discount products",
        ))

    elif intent == Intent.COUPON_INQUIRY:
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/coupons",
            params={"per_page": DEFAULT_PER_PAGE, "page": page},
            description="List available coupon codes",
        ))

    # ═══════════════════════════════════════════
    # ACCOUNT & ORDERING
    # ═══════════════════════════════════════════

    elif intent == Intent.SAVE_FOR_LATER:
        calls.append(WooAPICall(
            method="POST",
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
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Fetch product id={e.product_id} for order placement",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Fetch variations for order placement resolution of product id={e.product_id}",
            ))
        elif e.product_name or e.order_item_name:
            search_term = e.product_name or e.order_item_name
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": search_term, "status": "publish", "per_page": 5},
                description=f"Find product '{search_term}' for order placement",
            ))

    # ═══════════════════════════════════════════
    # FALLBACK
    # ═══════════════════════════════════════════

    if not calls:
        # If tag_slugs were resolved (e.g. by LLM fallback) but no branch above fired,
        # still apply the tag filter rather than ignoring it entirely.
        if e.tag_slugs:
            calls.append(_build_advanced_filter_call(
                tags=list(e.tag_slugs),
                page=page,
                description=f"Fallback tag search (slugs: {', '.join(e.tag_slugs)})",
            ))
        else:
            search = (
                e.product_name
                or e.search_term
                or next(iter(e.attributes.values()), None)
                or FALLBACK_SEARCH_TERM
            )
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": search, "per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish"},
                description=f"Fallback search: '{search}'",
            ))

    result.api_calls = calls
    return calls