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
        attr_label = (attr.get("attribute_label") or attr.get("name") or attr.get("attribute_name") or "").lower().strip()
        if attr_label == label_lower:
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

    # Only attach filters if there are actually conditions to evaluate
    if conditions:
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
    categories: set = None,
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
    search_term: str = None,
    product_id: int = None,
    requires_resolution: List[str] = None,
    in_stock: Optional[bool] = None,
) -> WooAPICall:
    
    conditions = []
    l = _loader() 

    # ── Tags (include) ──
    _or_pair_tag_slugs = {p.get("tag_slug") for p in (or_pairs or [])}
    _uncovered_tags = [t for t in (tags or []) if t not in _or_pair_tag_slugs]
    
    if _uncovered_tags:
        if tag_operator == "AND" and len(_uncovered_tags) > 1:
            # 🚨 WP tax_query bug workaround 🚨
            # Split multiple AND tags into separate IN conditions. 
            # Because the root API relation is AND, this forces strict intersection.
            for tag_slug in _uncovered_tags:
                conditions.append(_make_condition("product_tag", [tag_slug], "IN"))
        else:
            # Single tag or standard OR logic
            conditions.append(_make_condition("product_tag", _uncovered_tags, "IN"))
                    
    # ── Tags (exclude) ──
    if excluded_tags:
        conditions.append(_make_condition("product_tag", list(excluded_tags), "NOT IN"))
        
    # ── Categories (include) ──
    if categories:
        # Group categories by their base prefix to solve the AND vs OR dilemma!
        grouped_cats = {}
        for cat_slug in categories:
            # E.g., 'floor-2' -> 'floor', 'floor-exterior' -> 'floor'
            base_slug = cat_slug.split('-')[0] 
            if base_slug not in grouped_cats:
                grouped_cats[base_slug] = []
            grouped_cats[base_slug].append(cat_slug)
            
        for base_slug, slugs in grouped_cats.items():
            # Distinct terms ("exterior", "pavers") become separate AND conditions.
            # Duplicate terms ("floor", "floor-2", "floor-exterior") get bundled into a single OR array!
            conditions.append(_make_condition("product_cat", slugs, "IN"))

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
        value_groups = {}
        for attr_taxonomy, terms_value in attributes.items():
            raw_terms = terms_value if isinstance(terms_value, str) else ",".join(terms_value)
            val_key = raw_terms.lower().strip()
            if val_key not in value_groups:
                value_groups[val_key] = []
            value_groups[val_key].append(attr_taxonomy)

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
            if len(or_conditions) == 1:
                conditions.append(or_conditions[0])
            elif len(or_conditions) > 1:
                conditions.append(_make_or_group(or_conditions))
       
    # ── Ambiguous tag+attribute OR pairs ──
    if or_pairs:
        for pair in or_pairs:
            cat_slugs     = pair.get("cat_slugs", []) 
            tag_slug      = pair.get("tag_slug", "")
            attr_taxonomy = pair.get("attr_taxonomy", "")
            raw_attr_term = pair.get("attr_term", "")
            
            if attr_taxonomy and not attr_taxonomy.startswith("pa_"):
                resolved_slug = _attr_slug_for_label(attr_taxonomy)
                if resolved_slug:
                    attr_taxonomy = resolved_slug
            
            term_slug = raw_attr_term.lower().replace(" ", "-") if raw_attr_term else ""
            if attr_taxonomy and raw_attr_term and l:
                fetched_slug = l.get_attribute_term_slug(attr_taxonomy, raw_attr_term)
                if fetched_slug: 
                    term_slug = fetched_slug
                    
            or_conditions = []
            if tag_slug:
                or_conditions.append(_make_condition("product_tag", [tag_slug], "IN"))
            if cat_slugs:
                or_conditions.append(_make_condition("product_cat", cat_slugs, "IN"))
            if attr_taxonomy and term_slug:
                or_conditions.append(_make_condition(attr_taxonomy, [term_slug], "IN"))
                
            if len(or_conditions) >= 2:
                conditions.append(_make_or_group(or_conditions))
                
    # Cross-Taxonomy Overlap Merger

    def _normalize_term(t):
        t = str(t).lower().strip()
        # Ensure plurals like 'mosaics' cluster seamlessly with singulars like 'mosaic'
        if t.endswith('s') and not t.endswith('ss'):
            return t[:-1]
        return t

    flattened_in_conditions = []
    other_conditions = []

    for cond in conditions:
        if cond.get("operator") == "IN" and "terms" in cond and len(cond["terms"]) > 0:
            flattened_in_conditions.append(cond)
        elif cond.get("relation") == "OR":
            all_ins = True
            base_terms = set()
            for sub in cond.get("conditions", []):
                if not (sub.get("operator") == "IN" and "terms" in sub and len(sub["terms"]) > 0):
                    all_ins = False
                    break
                base_terms.add(_normalize_term(sub["terms"][0]))
                
            # Only flatten OR groups if all their terms are semantically identical.
            # This protects mixed-term intentional OR pairs (like "black" attr OR "black-look" tag).
            if all_ins and len(base_terms) == 1:
                for sub in cond.get("conditions", []):
                    flattened_in_conditions.append(sub)
            else:
                other_conditions.append(cond)
        else:
            other_conditions.append(cond)

    term_groups = {}
    for cond in flattened_in_conditions:
        # Group by the normalized first term (e.g., "mosaics" -> "mosaic")
        base_term = _normalize_term(cond["terms"][0])
        if base_term not in term_groups:
            term_groups[base_term] = []
        if cond not in term_groups[base_term]:
            term_groups[base_term].append(cond)

    final_conditions = list(other_conditions)
    for base_term, group in term_groups.items():
        if len(group) == 1:
            final_conditions.append(group[0])
        else:
            final_conditions.append(_make_or_group(group))

    conditions = final_conditions
    # ─── End Cross-Taxonomy Merger ───


    body = _serialize_query(conditions, page, per_page, min_price=min_price, max_price=max_price)

    if in_stock is True:
        body["stock_status"] = "instock"
    elif in_stock is False:
        body["stock_status"] = "outofstock"

    if product_id:
        body["ids"] = [product_id]
        body.pop("stock_status", None)
        # Clear taxonomy conditions as they are redundant
        body.pop("filters", None)
    
    elif search_term:
        logger.info(f"Ignored leftover search_term='{search_term}' — relying strictly on taxonomy.")

    import json as built_in_json
    logger.debug(
        f"api_builder: Executing advanced filter with body: {built_in_json.dumps(body)}"
    )

    return WooAPICall(
        method="POST",
        endpoint=f"{CUSTOM_API_BASE}/products-advanced-new",
        params={}, 
        body=body,
        description=description or "Advanced product filter",
        is_custom_api=True,
        requires_resolution=requires_resolution or []
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
        
        # Normalize attributes into a clean flat dictionary regardless of API source
        var_attrs_norm = {}
        raw_attrs = variation.get("attributes", {})
        if isinstance(raw_attrs, dict):
            for k, v in raw_attrs.items():
                clean_k = k.replace("attribute_", "").replace("pa_", "").replace("-", " ").strip().lower()
                clean_v = str(v).replace("-", " ").strip().lower()
                var_attrs_norm[clean_k] = clean_v
        elif isinstance(raw_attrs, list):
            for a in raw_attrs:
                clean_k = a.get("name", "").replace("-", " ").strip().lower()
                clean_v = a.get("option", "").replace("-", " ").strip().lower()
                var_attrs_norm[clean_k] = clean_v
                
        # Compare with requested entities
        for ent_label, ent_value in entities.attributes.items():
            ent_k = ent_label.replace("-", " ").strip().lower()
            ent_v = re.sub(r'[\"\'`]', '', ent_value).strip().lower().replace("-", " ")
            
            for v_k, v_v in var_attrs_norm.items():
                if ent_k in v_k or v_k in ent_k:
                    if ent_v == v_v or ent_v in v_v or v_v in ent_v:
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
        if getattr(e, "date_before", None):
            _order_params["before"] = e.date_before
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params=_order_params,
            description=f"Get customer orders{' after ' + e.date_after[:10] if getattr(e, 'date_after', None) else ' (last ' + str(count) + ')'}",
            requires_resolution=["customer_id"],
        ))

    elif intent == Intent.REORDER:
        if e.order_id:
            # If the user specified an order number, fetch that exact order
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders/{e.order_id}",
                params={},
                description=f"Fetch order #{e.order_id} for reorder (step 1)",
                requires_resolution=["reorder_step2"],
            ))
        else:
            # If they just said "reorder", default to their most recent order
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/orders",
                params={"customer": "CURRENT_USER_ID", "per_page": 1, "orderby": "date", "order": "desc"},
                description="Fetch last order for reorder (step 1)",
                requires_resolution=["customer_id", "reorder_step2"],
            ))
        
    elif intent == Intent.HISTORICAL_SEARCH:
        params = {"customer": "CURRENT_USER_ID", "orderby": "date", "order": "desc"}
        
        if getattr(e, 'order_id', None):
            params["include"] = [e.order_id]
        elif getattr(e, 'order_count', None):
            params["per_page"] = e.order_count
        else:
            params["per_page"] = 20
            
        calls.append(WooAPICall(
            method="GET",
            endpoint=f"{BASE}/orders",
            params=params,
            description="Fetch past orders to find a historical seed product",
            requires_resolution=["customer_id"],
        ))

    elif intent == Intent.ORDER_ITEM:
        product_name = e.order_item_name or e.product_name or ""
        
        attr_filters = {}
        for label, value in e.attributes.items():
            slug = _attr_slug_for_label(label)
            if slug and value:
                attr_filters[slug] = value
                
        if e.product_id or product_name:
            calls.append(_build_advanced_filter_call(
                product_id=e.product_id,
                search_term=product_name if not e.product_id else None,
                attributes=attr_filters if attr_filters else None,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                description=f"Find product '{product_name}' for ordering",
                requires_resolution=[] if e.product_id else ["order_item_step2"]
            ))

    elif intent == Intent.QUICK_ORDER:
        search_term = e.order_item_name or e.product_name or ""
        
        attr_filters = {}
        for label, value in e.attributes.items():
            slug = _attr_slug_for_label(label)
            if slug and value:
                attr_filters[slug] = value
                
        if e.product_id or search_term:
            calls.append(_build_advanced_filter_call(
                product_id=e.product_id,
                search_term=search_term if not e.product_id else None,
                attributes=attr_filters if attr_filters else None,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                description=f"Find product '{search_term}' for quick order",
                requires_resolution=[] if e.product_id else ["create_order_from_product"]
            ))

    # ═══════════════════════════════════════════
    # CATEGORY-BASED BROWSING
    # ═══════════════════════════════════════════

    elif intent == Intent.CATEGORY_BROWSE:
        if not e.target_category_slugs:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/categories",
                params={"per_page": 100, "page": page, "hide_empty": True, "orderby": "name", "order": "asc"},
                description="List all product categories (no category specified)",
            ))
        else:
            loader = get_store_loader()
            attr_filters = {}
            if e.attribute_slug and e.attributes and loader and loader.all_attributes_raw:
                for attr in loader.all_attributes_raw:
                    if attr.get("taxonomy") == e.attribute_slug:
                        label = attr.get("attribute_label", "").lower().strip()
                        term_value = e.attributes.get(label, "")
                        if term_value:
                            attr_filters[e.attribute_slug] = term_value
                        break

            calls.append(_build_advanced_filter_call(
                tags=list(e.tag_slugs) if e.tag_slugs else None,
                categories=e.target_category_slugs,
                attributes=attr_filters if attr_filters else None,
                page=page,
                excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
                excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
                excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
                tag_operator=e.tag_operator,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                description=f"Browse category '{e.category_name}'",
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
        calls.append(_build_advanced_filter_call(
            product_id=e.product_id,
            search_term=e.product_name if not e.product_id else None,
            page=page,
            in_stock=e.in_stock,
            description=f"List products (Product ID: {e.product_id}, Name: {e.product_name})"
        ))

    elif intent == Intent.PRODUCT_SEARCH:
        attr_filters = {}
        for label, value in e.attributes.items():
            slug = _attr_slug_for_label(label)
            if slug and value:
                attr_filters[slug] = value

        active_or_pairs = list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else []

        actual_search = e.product_name or e.search_term
        if not actual_search and not e.tag_slugs and not e.target_category_slugs and not attr_filters and not e.product_id and not active_or_pairs:
            actual_search = user_message

        calls.append(_build_advanced_filter_call(
            tags=list(e.tag_slugs) if e.tag_slugs else None,
            categories=e.target_category_slugs,
            attributes=attr_filters,
            or_pairs=active_or_pairs,
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            tag_operator=e.tag_operator,
            page=page,
            description=f"Advanced product search: '{actual_search}'",
            min_price=e.min_price,
            max_price=e.max_price,
            in_stock=e.in_stock,
            search_term=actual_search,
            product_id=e.product_id
        ))
        
    elif intent == Intent.PRODUCT_DETAIL:
        calls.append(_build_advanced_filter_call(
            product_id=e.product_id,
            search_term=e.product_name if not e.product_id else None,
            page=page,
            description=f"Get details for product '{e.product_name}'"
        ))

    elif intent == Intent.PRODUCT_ATTRIBUTE_INFO:
        calls.append(_build_advanced_filter_call(
            product_id=e.product_id,
            search_term=e.product_name if not e.product_id else None,
            page=page,
            description=f"Fetch product '{e.product_name}' for attribute info"
        ))

    elif intent == Intent.PRODUCT_BY_COLLECTION:
        calls.append(_build_advanced_filter_call(
            tags=list(e.tag_slugs) if e.tag_slugs else None,
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            tag_operator=e.tag_operator,
            or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
            page=page,
            description=f"Products from {e.collection_year} collection",
            min_price=e.min_price,
            max_price=e.max_price,
        ))

    elif intent == Intent.PRODUCT_BY_TAG:
        calls.append(_build_advanced_filter_call(
            tags=list(e.tag_slugs) if e.tag_slugs else None,
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            tag_operator=e.tag_operator,
            or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
            page=page,
            description=f"Products by tag (slugs: {','.join(e.tag_slugs or [])})",
            min_price=e.min_price,
            max_price=e.max_price,
        ))

    elif intent == Intent.PRODUCT_QUICK_SHIP:
        calls.append(_build_advanced_filter_call(
            tags=[TAG_SLUG_QUICK_SHIP],
            page=page,
            description="Quick ship / in-stock products"
        ))

    elif intent == Intent.RELATED_PRODUCTS:
        if e.product_name:
            calls.append(_build_advanced_filter_call(
                search_term=e.product_name,
                page=1,
                per_page=1,
                description=f"Find '{e.product_name}' to get related_ids"
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

        actual_search = e.product_name or e.search_term
        if not actual_search and not deduped_tag_slugs and not e.target_category_slugs and not attr_filters and not e.product_id and not e.attr_tag_or_pairs:
            actual_search = user_message

        calls.append(_build_advanced_filter_call(
            tags=deduped_tag_slugs if deduped_tag_slugs else None,
            attributes=attr_filters if attr_filters else None,
            or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
            tag_operator=e.tag_operator,
            page=page,
            in_stock=e.in_stock,
            min_price=e.min_price,
            max_price=e.max_price,
            categories=e.target_category_slugs,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            description=f"Filter by {attr_label}: {attr_value}",
            search_term=actual_search,
            product_id=e.product_id
        ))

    # ═══════════════════════════════════════════
    # VARIATIONS
    # ═══════════════════════════════════════════

    elif intent == Intent.PRODUCT_VARIATIONS:
        
        # 1. Safely build the attribute filters
        attr_filters = {}
        for label, value in e.attributes.items():
            slug = _attr_slug_for_label(label)
            if slug and value:
                attr_filters[slug] = value
                
        # 2. Pass EVERYTHING into the API call so series tags aren't dropped
        calls.append(_build_advanced_filter_call(
            product_id=e.product_id,
            search_term=e.product_name if not e.product_id else None,
            attributes=attr_filters if attr_filters else None,
            or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
            tags=list(e.tag_slugs) if e.tag_slugs else None,
            categories=e.target_category_slugs,
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
            excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
            tag_operator=e.tag_operator,
            page=page,
            in_stock=e.in_stock,
            description=f"Get specific variations for '{e.product_name or 'Series'}'"
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
        calls.append(_build_advanced_filter_call(
            search_term="bulk",
            page=page,
            description="Check for bulk discount products"
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
        search_term = e.product_name or e.order_item_name
        
        attr_filters = {}
        for label, value in e.attributes.items():
            slug = _attr_slug_for_label(label)
            if slug and value:
                attr_filters[slug] = value
                
        if e.product_id or search_term:
            calls.append(_build_advanced_filter_call(
                product_id=e.product_id,
                search_term=search_term if not e.product_id else None,
                attributes=attr_filters if attr_filters else None,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                page=1,
                description=f"Find product '{search_term}' for order placement"
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
        )
        
        if search:
            # We have SOME text extracted, so we can attempt a semantic fallback search
            logger.warning(
                f"api_builder: No calls built for intent={intent.value} — using fallback search | "
                f"search={search!r}"
            )
            calls.append(_build_advanced_filter_call(
                search_term=search,
                page=page,
                description=f"Fallback search: '{search}'"
            ))
        else:
            # We have absolutely nothing to search for. 
            # Do NOT force a query for "products". Let it return empty calls.
            logger.warning(
                f"api_builder: No calls built for intent={intent.value} and NO search terms found. "
                "Bypassing API call to trigger empty result handling."
            )

    result.api_calls = calls
    for call in calls:
        if not call.user_message:
            call.user_message = user_message
        if not call.session_id:
            call.session_id = session_id
    return calls