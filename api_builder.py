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
import re

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
#
# Internal representation: a flat list of condition dicts.
# Each condition:
#   {"taxonomy": str, "field": "slug", "terms": List[str], "operator": str}
#
# Supported operators:
#   "IN"     → product has at least one of these terms  (OR logic)
#   "AND"    → product must have ALL of these terms
#   "NOT IN" → product must not have any of these terms
#
# Serialization is isolated in _serialize_query() so tomorrow's API format
# swap is a ~10-line change in ONE function only.
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

    A node is either:
      - A flat condition: {"taxonomy": ..., "terms": ..., "operator": ...}
      - A nested group:   {"relation": "OR", "conditions": [...]}
    """
    if "conditions" in condition:
        return {
            "relation": condition["relation"],
            "conditions": [_serialize_condition(sub) for sub in condition["conditions"]],
        }
    # Flat condition — return without "field" key (new API format)
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

    Format:
        {
            "page": 1, "per_page": 4,
            "price": {"min": 20, "max": 80},   # optional
            "filters": {
                "relation": "AND",
                "conditions": [...]
            }
        }
    """
    body = {
        "page": page,
        "per_page": per_page,
    }

    # Price range — only include bounds that were specified
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
    attributes: dict = None,
    excluded_tags: List[str] = None,
    excluded_categories: List[str] = None,
    tag_operator: str = "AND",
    or_pairs: list = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    description: str = "",
    min_price: float = None,
    max_price: float = None,
) -> WooAPICall:
    """
    Build a single WooAPICall for the new products-advanced-new endpoint.

    Translates flat entity lists into a query condition tree then serializes
    to the current POST body format via _serialize_query().

    Args:
        tags:                Tag slugs to include.
        categories:          Category slugs to include (AND'd with other conditions).
        attributes:          {pa_taxonomy: term_value} attribute filters.
        excluded_tags:       Tag slugs to exclude (NOT IN).
        excluded_categories: Category slugs to exclude (NOT IN).
        tag_operator:        "AND" (must have all tags) or "OR" (must have any tag).
                             Maps to "AND" or "IN" in the query tree.
        or_pairs:            List of {tag_slug, attr_taxonomy, attr_term} dicts from
                             entities.attr_tag_or_pairs. Each pair becomes a nested OR
                             condition: OR(tag:slug, pa_taxonomy:term) so products are
                             found whether they use the tag or the attribute to express
                             the property. e.g. "glossy finish" →
                               OR(post_tag:glossy-finish, pa_finish:Glossy)
    """
    conditions = []

    # ── Tags (include) ───────────────────────────────────────────────────────
    # Only add a flat tag condition for tags NOT already covered by an or_pair.
    # or_pairs emit a nested OR(tag:slug, pa_taxonomy:term) per tag, so adding
    # the same slugs again as a hard AND condition would override those ORs —
    # any product missing either tag would be excluded even if it matched via
    # the attribute side of the OR pair.
    _or_pair_tag_slugs = {p.get("tag_slug") for p in (or_pairs or [])}
    _uncovered_tags = [t for t in (tags or []) if t not in _or_pair_tag_slugs]
    if _uncovered_tags:
        # AND only makes sense with multiple tags; a single tag always uses IN
        op = "AND" if (tag_operator == "AND" and len(_uncovered_tags) > 1) else "IN"
        conditions.append(_make_condition("product_tag", _uncovered_tags, op))
        logger.debug(f"api_builder: Added tag condition | tags={_uncovered_tags} | operator={op}")
    if _or_pair_tag_slugs:
        logger.debug(f"api_builder: Skipping flat AND for or_pair-covered tags={list(_or_pair_tag_slugs)}")

    # ── Tags (exclude) ───────────────────────────────────────────────────────
    if excluded_tags:
        conditions.append(_make_condition("product_tag", list(excluded_tags), "NOT IN"))

    # ── Categories (include) ─────────────────────────────────────────────────
    if categories:
        conditions.append(_make_condition("product_cat", list(categories), "IN"))

    # ── Categories (exclude) ─────────────────────────────────────────────────
    if excluded_categories:
        conditions.append(_make_condition("product_cat", list(excluded_categories), "NOT IN"))

    # ── Attributes ───────────────────────────────────────────────────────────
    # Each attribute is its own condition so different attributes are AND'd.
    # Terms within a single attribute are comma-separated and treated as IN.
    if attributes:
        for attr_taxonomy, terms_value in attributes.items():
            terms_list = (
                [t.strip() for t in terms_value.split(",") if t.strip()]
                if isinstance(terms_value, str)
                else list(terms_value)
            )
            if terms_list:
                conditions.append(_make_condition(attr_taxonomy, terms_list, "IN"))

    # ── Ambiguous tag+attribute OR pairs ─────────────────────────────────────
    # Each pair wraps two equivalent conditions in a nested OR so the API
    # matches products regardless of which representation they use.
    # e.g. "glossy finish" → OR(post_tag:glossy-finish, pa_finish:Glossy)
    if or_pairs:
        for pair in or_pairs:
            tag_slug      = pair.get("tag_slug", "")
            attr_taxonomy = pair.get("attr_taxonomy", "")
            attr_term     = pair.get("attr_term", "")
            if tag_slug and attr_taxonomy and attr_term:
                conditions.append(_make_or_group([
                    _make_condition("product_tag",    [tag_slug],   "IN"),
                    _make_condition(attr_taxonomy, [attr_term],  "IN"),
                ]))

    body = _serialize_query(conditions, page, per_page, min_price=min_price, max_price=max_price)

    logger.debug(
        f"api_builder: Built advanced filter | description={description!r} | "
        f"tags={tags} | categories={categories} | attributes={attributes} | "
        f"excluded_tags={excluded_tags} | tag_operator={tag_operator} | "
        f"or_pairs={or_pairs} | body={body}"
    )

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


def build_api_calls(result: ClassifiedResult, page: int = 1, user_message: str = "", session_id: str = "", customer_id: Optional[int] = None) -> List[WooAPICall]:
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

        # ── No category resolved — fall back to listing all categories ──
        if not cat_id:
            calls.append(WooAPICall(
                method="GET",
                endpoint=f"{BASE}/products/categories",
                params={"per_page": 100, "page": page, "hide_empty": True,
                        "orderby": "name", "order": "asc"},
                description="List all product categories (no category specified)",
            ))
        else:
            # cat_id is guaranteed truthy here
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
                excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
                excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
                tag_operator=e.tag_operator,
                or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
                page=page,
                description=f"Category-scoped search: '{e.product_name}' in '{e.category_name}'",
                min_price=e.min_price,
                max_price=e.max_price,
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
                excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
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
                excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
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
        # Origin can be expressed as a tag ("made-in-sri-lanka") OR an attribute
        # (pa_origin: "sri-lanka") — products may have one or the other.
        # Build OR pairs so both are searched, same pattern as finish/color.
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

        # AND-filter across extra categories (multi-category queries).
        # Each extra category gets its own IN condition so all must match.
        extra_conditions_categories = []
        for extra_cid in (e.extra_category_ids or []):
            if loader:
                extra_slugs = loader.get_all_slugs_for_category(extra_cid)
                if extra_slugs:
                    extra_conditions_categories.extend(extra_slugs)

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
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
            tag_operator=e.tag_operator,
            or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
            description=f"Filter by {attr_label}: {attr_value}",
            min_price=e.min_price,
            max_price=e.max_price,
        ))
        
    elif intent == Intent.FILTER_BY_ORIGIN:
        # Origin can be expressed as a tag ("made-in-sri-lanka") OR an attribute
        # (pa_origin: "sri-lanka") depending on how products were entered in the store.
        # Build an OR pair so both paths are searched — same pattern as finish/color.
        origin = e.attributes.get("origin", "")
        origin_or_pairs = []
        if e.attribute_slug and origin and e.tag_slugs:
            # Wrap tag + attribute as OR pairs — one per tag slug
            for tag_slug in e.tag_slugs:
                origin_or_pairs.append({
                    "tag_slug":      tag_slug,
                    "attr_taxonomy": e.attribute_slug,
                    "attr_term":     origin,
                })
        calls.append(_build_advanced_filter_call(
            # Don't pass tags/attributes separately — they're in or_pairs now
            tags=None if origin_or_pairs else (list(e.tag_slugs) if e.tag_slugs else None),
            attributes=None if origin_or_pairs else ({e.attribute_slug: origin} if (e.attribute_slug and origin) else None),
            or_pairs=origin_or_pairs if origin_or_pairs else (list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None),
            excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
            tag_operator=e.tag_operator,
            page=page,
            description=f"Filter by origin: {origin}",
            min_price=e.min_price,
            max_price=e.max_price,
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

    # ─── UPDATE_CUSTOMER ─────────────────────────────────────────────────────
    elif intent == Intent.UPDATE_CUSTOMER:
        # PUT /wp-json/wc/v3/customers/{customer_id}
        # role and email are never sent even if extracted.
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
    # Stamp every call with request context for api.txt logging
    for call in calls:
        if not call.user_message:
            call.user_message = user_message
        if not call.session_id:
            call.session_id = session_id
    return calls