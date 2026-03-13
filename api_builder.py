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
from chat_logger import get_logger

logger = get_logger("miraq_chat")

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


# ─────────────────────────────────────────────────────────────────────────────
# QUERY TREE BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _make_condition(taxonomy: str, terms: List[str], operator: str = "IN") -> dict:
    """Build a single filter condition node."""
    return {
        "taxonomy": taxonomy,
        "field": "slug",
        "terms": terms,
        "operator": operator,
    }


def _serialize_condition(condition: dict) -> dict:
    """
    Recursively serialize a single condition node.
    """
    if "conditions" in condition:
        return {
            "relation": condition["relation"],
            "conditions": [_serialize_condition(sub) for sub in condition["conditions"]],
        }
    return {
        "taxonomy": condition["taxonomy"],
        "terms":    condition["terms"],
        "operator": condition["operator"],
    }


def _serialize_query(
    conditions: list,
    page: int,
    per_page: int,
    min_price: float = None,
    max_price: float = None,
) -> dict:
    """
    Serialize a list of condition nodes to the POST body format.
    """
    body = {
        "page": page,
        "per_page": per_page,
    }

    if min_price is not None or max_price is not None:
        price = {}
        if min_price is not None:
            price["min"] = min_price
        if max_price is not None:
            price["max"] = max_price
        body["price"] = price

    body["filters"] = {
        "relation": "AND",
        "conditions": [_serialize_condition(c) for c in conditions],
    }
    return body


def _make_or_group(conditions: list) -> dict:
    """Wrap a list of conditions in a nested OR group node."""
    return {"relation": "OR", "conditions": conditions}


def _build_advanced_filter_call(
    tags: List[str] = None,
    categories: List[str] = None,
    extra_categories: List[List[str]] = None, 
    attributes: dict = None,
    excluded_tags: List[str] = None,
    excluded_categories: List[str] = None,
    excluded_attributes: dict = None, 
    tag_operator: str = "AND",
    or_pairs: list = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    description: str = "",
    min_price: float = None,
    max_price: float = None,
) -> WooAPICall:
    
    conditions = []
    l = _loader() 

    # ── Tags (include) ──
    _or_pair_tag_slugs = {p.get("tag_slug") for p in (or_pairs or [])}
    _uncovered_tags = [t for t in (tags or []) if t not in _or_pair_tag_slugs]
    if _uncovered_tags:
        op = "AND" if (tag_operator == "AND" and len(_uncovered_tags) > 1) else "IN"
        conditions.append(_make_condition("product_tag", _uncovered_tags, op))

    # ── Tags (exclude) ──
    if excluded_tags:
        conditions.append(_make_condition("product_tag", list(excluded_tags), "NOT IN"))

    # ── Categories (include) ──
    if categories:
        conditions.append(_make_condition("product_cat", list(categories), "IN"))

    # ── Extra Categories (AND logic) ── 
    if extra_categories:
        for extra_slug_list in extra_categories:
            if extra_slug_list:
                conditions.append(_make_condition("product_cat", extra_slug_list, "IN"))

    # ── Categories (exclude) ──
    if excluded_categories:
        conditions.append(_make_condition("product_cat", list(excluded_categories), "NOT IN"))

    # ── Attributes (exclude) ──
    if excluded_attributes:
        for attr_taxonomy, slug_list in excluded_attributes.items():
            if slug_list:
                conditions.append(_make_condition(attr_taxonomy, slug_list, "NOT IN"))

    # ── Attributes (include) ──
    if attributes:
        # Step 1: Group attribute labels by their requested value
        value_groups = {}
        for attr_taxonomy, terms_value in attributes.items():
            raw_terms = terms_value if isinstance(terms_value, str) else ",".join(terms_value)
            val_key = raw_terms.lower().strip()
            
            if val_key not in value_groups:
                value_groups[val_key] = []
            value_groups[val_key].append(attr_taxonomy)

        # Step 2: Build the conditions
        for val_key, taxonomies in value_groups.items():
            raw_terms_list = [t.strip() for t in val_key.split(",") if t.strip()]
            
            or_conditions = []
            for taxonomy in taxonomies:
                slug_list = []
                for raw_term in raw_terms_list:
                    term_slug = l.get_attribute_term_slug(taxonomy, raw_term) if l else None
                    if term_slug:
                        slug_list.append(term_slug)
                    else:
                        slug_list.append(raw_term.replace(" ", "-").replace('"', '').replace("'", ""))
                
                if slug_list:
                    or_conditions.append(_make_condition(taxonomy, slug_list, "IN"))
            
            # Step 3: Apply AND vs OR logic
            if len(or_conditions) == 1:
                # Unique value -> standard AND condition
                conditions.append(or_conditions[0])
            elif len(or_conditions) > 1:
                # Same value across multiple taxonomies -> OR condition
                conditions.append(_make_or_group(or_conditions))
                              
    # ── Ambiguous tag+attribute OR pairs ──
    if or_pairs:
        for pair in or_pairs:
            tag_slug      = pair.get("tag_slug", "")
            attr_taxonomy = pair.get("attr_taxonomy", "")
            raw_attr_term = pair.get("attr_term", "")
            
            if tag_slug and attr_taxonomy and raw_attr_term:
                term_slug = l.get_attribute_term_slug(attr_taxonomy, raw_attr_term) if l else None
                if not term_slug:
                     term_slug = raw_attr_term.lower().replace(" ", "-")

                conditions.append(_make_or_group([
                    _make_condition("product_tag", [tag_slug], "IN"),
                    _make_condition(attr_taxonomy, [term_slug], "IN"),
                ]))

    body = _serialize_query(conditions, page, per_page, min_price=min_price, max_price=max_price)

    return WooAPICall(
        method="POST",
        endpoint=f"{CUSTOM_API_BASE}/products-advanced-new",
        params={},
        body=body,
        description=description or "Advanced product filter",
        is_custom_api=True,
    )


def match_variation_to_entities(variations: list, entities) -> Optional[dict]:
    """
    Given a list of WooCommerce variation dicts and extracted entities, return
    the variation that best matches the user's requested attributes.
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
                    ent_clean = re.sub(r'[\"\'`]', '', ent_value).strip().lower()
                    opt_clean = re.sub(r'[\"\'`]', '', attr_option).strip().lower()
                    if ent_clean == opt_clean or ent_clean in opt_clean or opt_clean in ent_clean:
                        score += 1
                        break
        if score > best_score:
            best_score = score
            best_variation = variation

    return best_variation if best_score > 0 else None


def build_api_calls(result: ClassifiedResult, page: int = 1, user_message: str = "", session_id: str = "", customer_id: Optional[int] = None) -> List[WooAPICall]:
    """Build one or more WooCommerce API calls from classified result."""
    intent = result.intent
    e = result.entities
    calls = []

    # ═══════════════════════════════════════════
    # GREETING 
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

        if not cat_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/categories",
                params={"per_page": 100, "page": page, "hide_empty": True,
                        "orderby": "name", "order": "asc"},
                description="List all product categories (no category specified)",
            ))
        else:
            if loader:
                categories_list = loader.get_all_slugs_for_category(cat_id)
            else:
                cat_slug = _category_slug(cat_id)
                categories_list = [cat_slug] if cat_slug else []

            tag_slugs = list(e.tag_slugs) if e.tag_slugs else []

            attr_filters = {}
            if e.attribute_slug and e.attributes:
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
                excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
                excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
                excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
                tag_operator=e.tag_operator,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                description=f"Browse category '{e.category_name}' (id={e.category_id})",
                min_price=e.min_price,
                max_price=e.max_price,
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
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Get details for product id={e.product_id} ('{e.product_name}')",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "status": "publish"},
                description=f"Get variations for '{e.product_name}'",
            ))
        elif e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish",
                        "search": e.product_name},
                description=f"Search products matching '{e.product_name}'",
            ))
        else:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish",
                        "stock_status": "instock", "orderby": "menu_order", "order": "asc"},
                description="List all published, in-stock products",
            ))

    elif intent == Intent.PRODUCT_SEARCH:
        has_attributes = bool(e.attributes)

        if e.product_name and e.category_id:
            loader = get_store_loader()
            cat_id = e.category_id
            if not cat_id and e.category_name and loader:
                cat_id = loader.get_category_id(e.category_name)
            categories_list = loader.get_all_slugs_for_category(cat_id) if (cat_id and loader) else []
            tag_slugs = list(e.tag_slugs) if e.tag_slugs else []
            attr_filters = {}
            if e.attribute_slug and e.attributes and loader and loader.all_attributes_raw:
                for attr in loader.all_attributes_raw:
                    if attr.get("taxonomy") == e.attribute_slug:
                        label = attr.get("attribute_label", "").lower().strip()
                        term_value = e.attributes.get(label, "")
                        if term_value:
                            attr_filters[e.attribute_slug] = term_value
                        break
            call = _build_advanced_filter_call(
                tags=tag_slugs if tag_slugs else None,
                categories=categories_list if categories_list else None,
                attributes=attr_filters if attr_filters else None,
                excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
                excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
                excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
                tag_operator=e.tag_operator,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                page=page,
                description=f"Category-scoped search: '{e.product_name}' in '{e.category_name}'",
                min_price=e.min_price,
                max_price=e.max_price,
            )
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
                excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
                excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
                tag_operator=e.tag_operator,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                page=page,
                description=f"Attribute-scoped search: {e.attributes}",
                min_price=e.min_price,
                max_price=e.max_price,
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
                excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
                excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
                tag_operator=e.tag_operator,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                page=page,
                description=f"Products from {e.collection_year} collection (tags: {','.join(e.tag_slugs)})",
                min_price=e.min_price,
                max_price=e.max_price,
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
                excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
                excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
                excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
                tag_operator=e.tag_operator,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                page=page,
                description=f"Products by tag (slugs: {','.join(e.tag_slugs)})",
                min_price=e.min_price,
                max_price=e.max_price,
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
        attr_slug = _attr_slug_for_label("origin") or e.attribute_slug
        origin_or_pairs = []
        if attr_slug and origin and e.tag_slugs:
            for tag_slug in e.tag_slugs:
                origin_or_pairs.append({
                    "tag_slug":      tag_slug,
                    "attr_taxonomy": attr_slug,
                    "attr_term":     origin,
                })
        calls.append(_build_advanced_filter_call(
            tags=None if origin_or_pairs else (list(e.tag_slugs) if e.tag_slugs else None),
            attributes=None if origin_or_pairs else ({attr_slug: origin} if (attr_slug and origin) else None),
            or_pairs=origin_or_pairs or (list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None),
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            tag_operator=e.tag_operator,
            page=page,
            description=f"Products from {origin}",
            min_price=e.min_price,
            max_price=e.max_price,
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

        extra_conditions_categories = []
        for extra_cid in (e.extra_category_ids or []):
            if loader:
                extra_slugs = loader.get_all_slugs_for_category(extra_cid)
                if extra_slugs:
                    extra_conditions_categories.append(extra_slugs)

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
            tags=deduped_tag_slugs if deduped_tag_slugs else None,
            attributes=attr_filters if attr_filters else None,
            or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
            tag_operator=e.tag_operator,
            page=page,
            min_price=e.min_price,
            max_price=e.max_price,
            categories=categories_list if categories_list else None,
            extra_categories=extra_conditions_categories if extra_conditions_categories else None,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            description=f"Filter by {attr_label}: {attr_value}",
        ))        

    elif intent == Intent.FILTER_BY_FINISH:
        finish_value = e.attributes.get("finish", "")
        finish_slug = _attr_slug_for_label("finish") or e.attribute_slug
        finish_or_pairs = []
        if finish_slug and finish_value and e.tag_slugs:
            finish_or_pairs = [
                {"attribute": finish_slug, "value": finish_value},
                {"tag": list(e.tag_slugs)[0]},
            ]

        cat_id = e.category_id
        loader = get_store_loader()
        if not cat_id and e.category_name and loader:
            cat_id = loader.get_category_id(e.category_name)
        categories_list = loader.get_all_slugs_for_category(cat_id) if (cat_id and loader) else []

        attr_filters = {}
        if finish_slug and finish_value:
            attr_filters[finish_slug] = finish_value

        calls.append(_build_advanced_filter_call(
            tags=list(e.tag_slugs) if e.tag_slugs else None,
            categories=categories_list if categories_list else None,
            attributes=attr_filters if attr_filters else None,
            page=page,
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            tag_operator=e.tag_operator,
            or_pairs=finish_or_pairs if finish_or_pairs else None,
            description=f"Filter by finish: {finish_value}",
            min_price=e.min_price,
            max_price=e.max_price,
        ))

        
    elif intent == Intent.FILTER_BY_ORIGIN:
        cat_id = e.category_id
        loader = get_store_loader()
        if not cat_id and e.category_name and loader:
            cat_id = loader.get_category_id(e.category_name)
        categories_list = loader.get_all_slugs_for_category(cat_id) if (cat_id and loader) else []

        origin = e.attributes.get("origin", "")
        origin_or_pairs = []
        if e.attribute_slug and origin and e.tag_slugs:
            for tag_slug in e.tag_slugs:
                origin_or_pairs.append({
                    "tag_slug":      tag_slug,
                    "attr_taxonomy": e.attribute_slug,
                    "attr_term":     origin,
                })
        calls.append(_build_advanced_filter_call(
            tags=None if origin_or_pairs else (list(e.tag_slugs) if e.tag_slugs else None),
            categories=categories_list if categories_list else None,
            attributes=None if origin_or_pairs else ({e.attribute_slug: origin} if (e.attribute_slug and origin) else None),
            or_pairs=origin_or_pairs if origin_or_pairs else (list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None),
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None, 
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None, 
            tag_operator=e.tag_operator,
            page=page,
            description=f"Filter by origin: {origin}",
            min_price=e.min_price,
            max_price=e.max_price,
        ))

    elif intent == Intent.SIZE_LIST:
        if e.product_id:
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
        if e.product_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}",
                params={},
                description=f"Get parent product '{e.product_name}' for sample sizes",
            ))
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/{e.product_id}/variations",
                params={"per_page": 100, "page": page, "status": "publish"},
                description=f"Get all variations for '{e.product_name}' (sample size check)",
            ))
        elif e.product_name:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products",
                params={"search": e.product_name, "status": "publish",
                        "type": "variable", "per_page": 5},
                description=f"Find variable product '{e.product_name}' for sample sizes",
            ))
        else:
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

    # ─── UPDATE_CUSTOMER ─────────────────────────────────────────────────────
    elif intent == Intent.UPDATE_CUSTOMER:
        if not customer_id:
            logger.warning("api_builder: UPDATE_CUSTOMER intent but no customer_id resolved")
        else:
            payload = {}
            for field_key, value in (e.customer_updates or {}).items():
                if field_key not in ("role", "email", "password"):
                    payload[field_key] = value
            if e.billing_updates:
                payload["billing"] = dict(e.billing_updates)
            if e.shipping_updates:
                payload["shipping"] = dict(e.shipping_updates)
            if payload:
                calls.append(WooAPICall(
                    method="PUT",
                    endpoint=f"{BASE}/customers/{customer_id}",
                    params={},
                    body=payload,
                    description=f"Update customer id={customer_id} | fields={list(payload.keys())}",
                ))
                logger.debug(
                    f"api_builder: UPDATE_CUSTOMER | customer_id={customer_id} | payload_keys={list(payload.keys())}"
                )
            else:
                logger.warning("api_builder: UPDATE_CUSTOMER payload empty after field filtering")

    # ═══════════════════════════════════════════
    # FALLBACK
    # ═══════════════════════════════════════════

    if not calls:
        search = (
            e.product_name
            or e.search_term
            or next(iter(e.attributes.values()), None)
            or FALLBACK_SEARCH_TERM
        )
        logger.warning(
            f"api_builder: No calls built for intent={intent.value} — using fallback search | "
            f"search={search!r} | product_name={e.product_name!r} | category_name={e.category_name!r}"
        )
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/products",
            params={"search": search, "per_page": DEFAULT_PER_PAGE, "page": page, "status": "publish"},
            description=f"Fallback search: '{search}'",
        ))

    result.api_calls = calls
    for call in calls:
        if not call.user_message:
            call.user_message = user_message
        if not call.session_id:
            call.session_id = session_id
    return calls