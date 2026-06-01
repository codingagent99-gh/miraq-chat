"""
api_builder — Builds WooCommerce API calls using live StoreLoader data.

Public API:
  - build_api_calls(result, page, ...) → List[WooAPICall]
  - match_variation_to_entities(variations, entities) → Optional[dict]
"""

import re
from typing import List, Optional

from models import Intent, ClassifiedResult, WooAPICall, ExtractedEntities
from app_config import CUSTOM_ORDER_ROLES, DEFAULT_PER_PAGE, DEFAULT_ORDER_PER_PAGE
from config.store_config import TAG_SLUG_QUICK_SHIP
from chat_logger import get_logger

from api_builder.store_helpers import (
    loader as _loader,
    attr_id as _attr_id,
    resolve_attr_filters,
)
from api_builder.filter_builder import build_advanced_filter_call
from ecommerce import endpoints

logger = get_logger("miraq_chat")

# ══════════════════════════════════════════════════════════════
# VARIATION MATCHER
# ══════════════════════════════════════════════════════════════

def match_variation_to_entities(variations: list, entities) -> list:
    if not variations or not entities.attributes:
        return []

    best_variations = []
    best_score = 0

    for variation in variations:
        score = 0
        var_attrs = _normalize_variation_attrs(variation)

        for ent_label, ent_value in entities.attributes.items():
            ent_k = ent_label.replace("-", " ").strip().lower()

            if isinstance(ent_value, (list, tuple, set)):
                ent_values = [str(v).strip().lower().replace("-", " ") for v in ent_value]
            else:
                raw = re.sub(r'\s+(?:and|&)\s+', ',', str(ent_value), flags=re.IGNORECASE)
                ent_values = [
                    re.sub(r'[\"\'`]', '', t).strip().lower().replace("-", " ")
                    for t in raw.split(",") if t.strip()
                ]

            for v_k, v_v in var_attrs.items():
                if not v_v:
                    continue
                if ent_k in v_k or v_k in ent_k:
                    for ent_v in ent_values:
                        if ent_v == v_v:
                            score += 10
                        elif ent_v in v_v or v_v in ent_v:
                            score += 1
                    break

        if score > best_score:
            best_score = score
            best_variations = [variation]
        elif score == best_score and score > 0:
            best_variations.append(variation)

    return best_variations

def _normalize_variation_attrs(variation: dict) -> dict:
    """Normalize variation attributes into a flat {clean_key: clean_value} dict."""
    result = {}
    raw = variation.get("attributes", {})
    if isinstance(raw, dict):
        for k, v in raw.items():
            # Normalize WooCommerce variation attribute keys. Three input formats:
            #   attribute_pa_color → color  |  attribute_color → color  |  pa_color → color
            ck = k.removeprefix("attribute_pa_").removeprefix("attribute_").removeprefix("pa_").replace("-", " ").strip().lower()
            cv = str(v).replace("-", " ").strip().lower()
            result[ck] = cv
    elif isinstance(raw, list):
        for a in raw:
            ck = a.get("name", "").replace("-", " ").strip().lower()
            cv = a.get("option", "").replace("-", " ").strip().lower()
            result[ck] = cv
    return result


# ═════════════════════════════════���════════════════════════════
# INTENT → API CALL ROUTER
# ══════════════════════════════════════════════════════════════

def build_api_calls(
    result: ClassifiedResult,
    page: int = 1,
    user_message: str = "",
    session_id: str = "",
    customer_id: Optional[int] = None,
    role=None
) -> List[WooAPICall]:
    """Build one or more WooCommerce API calls from classified result."""
    intent = result.intent
    e = result.entities
    calls: List[WooAPICall] = []

    # Dispatch to the appropriate builder
    _BUILDERS = {
        Intent.GREETING:              _build_greeting,
        Intent.LAST_ORDER:            _build_last_order,
        Intent.ORDER_HISTORY:         _build_order_history,
        Intent.REORDER:               _build_reorder,
        Intent.HISTORICAL_SEARCH:     _build_historical_search,
        Intent.ORDER_ITEM:            _build_order_item,
        Intent.QUICK_ORDER:           _build_quick_order,
        Intent.CATEGORY_BROWSE:       _build_category_browse,
        Intent.CATEGORY_LIST:         _build_category_list,
        Intent.PRODUCT_LIST:          _build_product_list,
        Intent.PRODUCT_SEARCH:        _build_product_search,
        Intent.PRODUCT_DETAIL:        _build_product_detail,
        Intent.PRODUCT_ATTRIBUTE_INFO: _build_product_attr_info,
        Intent.PRODUCT_BY_COLLECTION: _build_product_by_collection,
        Intent.PRODUCT_BY_TAG:        _build_product_by_tag,
        Intent.PRODUCT_QUICK_SHIP:    _build_product_quick_ship,
        Intent.RELATED_PRODUCTS:      _build_related_products,
        Intent.PRODUCT_CATALOG:       _build_product_catalog,
        Intent.PRODUCT_TYPES:         _build_product_types,
        Intent.FILTER_BY_ATTRIBUTE:   _build_filter_by_attribute,
        Intent.PRODUCT_VARIATIONS:    _build_product_variations,
        Intent.DISCOUNT_INQUIRY:      _build_discount_inquiry,
        Intent.BULK_DISCOUNT:         _build_bulk_discount,
        Intent.COUPON_INQUIRY:        _build_coupon_inquiry,
        Intent.SAVE_FOR_LATER:        _build_save_for_later,
        Intent.ORDER_TRACKING:        _build_order_tracking,
        Intent.ORDER_STATUS:          _build_order_tracking,  # same logic
        Intent.PLACE_ORDER:           _build_place_order,
        Intent.UPDATE_CUSTOMER:       _build_update_customer,
        Intent.CHECKOUT:              _build_checkout,
    }

    builder = _BUILDERS.get(intent)
    if builder:
        # Builders that need customer_id or user_message get them via kwargs
        if intent == Intent.UPDATE_CUSTOMER:
            calls = builder(e, page, customer_id=customer_id)
        # in build_api_calls dispatcher:
        elif intent in (Intent.PRODUCT_SEARCH, Intent.FILTER_BY_ATTRIBUTE, Intent.PRODUCT_DETAIL):
            calls = builder(e, page, user_message=user_message)
        elif intent in (Intent.LAST_ORDER, Intent.ORDER_HISTORY, Intent.HISTORICAL_SEARCH,
                Intent.REORDER, Intent.ORDER_TRACKING, Intent.ORDER_STATUS,
                Intent.ORDER_ITEM):
            calls = builder(e, page, role=role)
        else:
            calls = builder(e, page)

    # ── Fallback ──
    if not calls:
        calls = _build_fallback(e, page, intent, user_message)

    # ── Stamp metadata ──
    result.api_calls = calls
    for call in calls:
        if not call.user_message:
            call.user_message = user_message
        if not call.session_id:
            call.session_id = session_id
    return calls


# ══════════════════════════════════════════════════════════════
# INDIVIDUAL INTENT BUILDERS
# ══════════════════════════════════════════════════════════════

def _build_greeting(e, page) -> list:
    return []


# ─── Orders ───

def _build_last_order(e, page, customer_id=None, role=None) -> list:
    if role in CUSTOM_ORDER_ROLES:
        return [endpoints.list_cs_orders(
            body={"customer_id": "CURRENT_USER_ID", "page": 1, "per_page": 1},
            description="CS rep last order",
            requires_resolution=["customer_id"],
        )]

    return [endpoints.list_customer_orders(
        customer_id="CURRENT_USER_ID",
        page=1,
        per_page=1,
        description="Get the customer's most recent order",
        requires_resolution=["customer_id"],
    )]


def _build_order_history(e, page, customer_id=None, role=None) -> list:
    if role in CUSTOM_ORDER_ROLES:
        body = {"customer_id": "CURRENT_USER_ID", "page": page, "per_page": e.order_count or DEFAULT_ORDER_PER_PAGE}
        if getattr(e, "date_after", None): body["after"] = e.date_after
        if getattr(e, "date_before", None): body["before"] = e.date_before
        return [endpoints.list_cs_orders(
            body=body,
            description="CS rep order history",
            requires_resolution=["customer_id"],
        )]

    count = e.order_count or DEFAULT_ORDER_PER_PAGE
    extra = {}
    if getattr(e, "date_after", None):
        extra["after"] = e.date_after
    if getattr(e, "date_before", None):
        extra["before"] = e.date_before
    desc = f"Get customer orders{' after ' + e.date_after[:10] if getattr(e, 'date_after', None) else ' (last ' + str(count) + ')'}"
    return [endpoints.list_customer_orders(
        customer_id="CURRENT_USER_ID",
        page=page,
        per_page=count,
        description=desc,
        requires_resolution=["customer_id"],
        **extra,
    )]

def _build_reorder(e, page, role=None) -> list:
    if e.order_id:
        return [endpoints.fetch_order(
            order_id=e.order_id,
            description=f"Fetch order #{e.order_id} for reorder (step 1)",
            requires_resolution=["reorder_step2"],
        )]
    return [endpoints.list_customer_orders(
        customer_id="CURRENT_USER_ID",
        page=1,
        per_page=1,
        description="Fetch last order for reorder (step 1)",
        requires_resolution=["customer_id", "reorder_step2"],
    )]

def _build_historical_search(e, page, customer_id=None, role=None) -> list:
    if role in CUSTOM_ORDER_ROLES:
        body = {
            "customer_id": "CURRENT_USER_ID",
            "page":        page,
            "per_page":    e.order_count or 20,
        }
        if getattr(e, "date_after", None):
            body["after"] = e.date_after
        if getattr(e, "date_before", None):
            body["before"] = e.date_before
        return [endpoints.list_cs_orders(
            body=body,
            description="CS rep order history",
            requires_resolution=["customer_id"],
        )]

    extra = {}
    if getattr(e, 'order_id', None):
        extra["include"] = [e.order_id]
    extra["per_page"] = e.order_count if getattr(e, 'order_count', None) else 20
    if getattr(e, "date_after", None):
        extra["after"] = e.date_after
    if getattr(e, "date_before", None):
        extra["before"] = e.date_before

    return [endpoints.list_customer_orders(
        customer_id="CURRENT_USER_ID",
        page=page,
        per_page=extra.pop("per_page"),
        description="Fetch past orders to find a historical seed product",
        requires_resolution=["customer_id"],
        **extra,
    )]


def _build_order_item(e, page) -> list:
    product_name = e.order_item_name or e.product_name or ""
    attr_filters = resolve_attr_filters(e.attributes)
    if not (e.product_id or product_name):
        return []
    return [build_advanced_filter_call(
        product_id=e.product_id,
        search_term=product_name if not e.product_id else None,
        attributes=attr_filters or None,
        or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
        description=f"Find product '{product_name}' for ordering",
        requires_resolution=[] if e.product_id else ["order_item_step2"],
    )]


def _build_quick_order(e, page) -> list:
    search_term = e.order_item_name or e.product_name or ""
    attr_filters = resolve_attr_filters(e.attributes)
    if not (e.product_id or search_term):
        return []

    # Determine if any taxonomy-based filters are available
    has_taxonomy = bool(
        e.product_id
        or e.tag_slugs
        or e.target_category_slugs
        or attr_filters
        or e.attr_tag_or_pairs
    )

    if not has_taxonomy and search_term:
        # No taxonomy match — the custom filter endpoint will return garbage.
        # Fall back to standard WooCommerce text search.
        logger.info(f"_build_quick_order: No taxonomy signals for '{search_term}', falling back to WooCommerce text search")
        return [endpoints.search_products(
            search_term=search_term,
            page=page,
            per_page=DEFAULT_PER_PAGE,
            description=f"Text search for product '{search_term}' (quick order fallback)",
            requires_resolution=["create_order_from_product"],
        )]

    return [build_advanced_filter_call(
        product_id=e.product_id,
        search_term=search_term if not e.product_id else None,
        attributes=attr_filters or None,
        or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
        description=f"Find product '{search_term}' for quick order",
        requires_resolution=[] if e.product_id else ["create_order_from_product"],
    )]


# ─── Categories ───

def _build_category_browse(e, page) -> list:
    if not e.target_category_slugs:
        return [endpoints.list_categories(
            page=page,
            per_page=100,
            description="List all product categories (no category specified)",
            orderby="name",
            order="asc",
        )]

    loader = _loader()
    attr_filters = {}
    if e.attribute_slug and e.attributes and loader and loader.all_attributes_raw:
        for attr in loader.all_attributes_raw:
            if attr.get("taxonomy") == e.attribute_slug:
                label = attr.get("attribute_label", "").lower().strip()
                term_value = e.attributes.get(label, "")
                if term_value:
                    attr_filters[e.attribute_slug] = term_value
                break

    return [build_advanced_filter_call(
        tags=list(e.tag_slugs) if e.tag_slugs else None,
        categories=e.target_category_slugs,
        attributes=attr_filters or None,
        page=page,
        excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
        excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
        excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
        tag_operator=e.tag_operator,
        or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
        description=f"Browse category '{e.category_name}'",
        min_price=e.min_price, max_price=e.max_price,
    )]


def _build_category_list(e, page) -> list:
    return [endpoints.list_categories(
        page=page,
        per_page=100,
        description="List all product categories",
        orderby="name",
        order="asc",
    )]


# ─── Product discovery ───

def _common_exclusion_kwargs(e) -> dict:
    """Extract the common exclusion/filter kwargs from entities."""
    return dict(
        excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
        excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
        excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
        tag_operator=e.tag_operator,
        min_price=e.min_price,
        max_price=e.max_price,
    )
    
def _build_product_list(e, page) -> list:
    return [build_advanced_filter_call(
        product_id=e.product_id,
        search_term=e.product_name if not e.product_id else None,
        page=page, in_stock=e.in_stock,
        description=f"List products (Product ID: {e.product_id}, Name: {e.product_name})",
    )]

def _build_product_search(e, page, user_message: str = "") -> list:
    attr_filters = resolve_attr_filters(e.attributes)
    active_or_pairs = list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else []

    actual_search = e.product_name or e.search_term
    if not actual_search and not e.tag_slugs and not e.target_category_slugs and not attr_filters and not e.product_id and not active_or_pairs:
        actual_search = user_message

    # When a specific product_id is resolved, the endpoint finds that product
    # on page 1 of the product list — always. Passing page=2 moves past it
    # and returns nothing. Instead, keep the product query on page=1 and pass
    # variation_page for variation-level pagination.
    product_page = 1 if e.product_id else page
    variation_page = page if (e.product_id and page > 1) else None

    return [build_advanced_filter_call(
        tags=list(e.tag_slugs) if e.tag_slugs else None,
        categories=e.target_category_slugs,
        attributes=attr_filters,
        or_pairs=active_or_pairs,
        page=product_page,
        variation_page=variation_page,
        description=f"Advanced product search: '{actual_search}'",
        in_stock=e.in_stock,
        search_term=actual_search,
        product_id=e.product_id,
        **_common_exclusion_kwargs(e),
    )]


def _build_product_detail(e, page, user_message: str = "") -> list:
    search = e.product_name or (user_message if not e.product_id else None)
    if not search and not e.product_id:
        return []   # nothing to work with — let fallback handle it
    return [build_advanced_filter_call(
        product_id=e.product_id,
        search_term=search,
        page=page,
        description=f"Get details for product '{search}'",
    )]


def _build_product_attr_info(e, page) -> list:
    return [build_advanced_filter_call(
        product_id=e.product_id,
        search_term=e.product_name if not e.product_id else None,
        page=page,
        description=f"Fetch product '{e.product_name}' for attribute info",
    )]


def _build_product_by_collection(e, page) -> list:
    return [build_advanced_filter_call(
        tags=list(e.tag_slugs) if e.tag_slugs else None,
        page=page,
        description=f"Products from {e.collection_year} collection",
        **_common_exclusion_kwargs(e),
    )]

def _build_product_by_tag(e, page) -> list:
    return [build_advanced_filter_call(
        page=page,
        description=f"Products by tag (slugs: {','.join(e.tag_slugs or [])})",
        **e.get_filter_kwargs(),
    )]
    
def _build_product_quick_ship(e, page) -> list:
    return [build_advanced_filter_call(
        tags=[TAG_SLUG_QUICK_SHIP], page=page,
        description="Quick ship / in-stock products",
    )]


def _build_related_products(e, page) -> list:
    if not e.product_name:
        return []
    return [build_advanced_filter_call(
        search_term=e.product_name, page=1, per_page=1,
        description=f"Find '{e.product_name}' to get related_ids",
    )]


def _build_product_catalog(e, page) -> list:
    return [
        endpoints.list_categories(
            page=page,
            per_page=100,
            description="Get all product categories",
        ),
        endpoints.list_tags(
            page=page,
            per_page=100,
            description="Get all product tags",
        ),
    ]


def _build_product_types(e, page) -> list:
    l = _loader()
    if not l or not l.all_attributes_raw:
        return []
    for attr in l.all_attributes_raw:
        label = attr.get("attribute_label", "").lower()
        if "visual" in label or "type" in label:
            type_slug = attr.get("taxonomy")
            aid = _attr_id(type_slug) if type_slug else None
            if aid:
                return [endpoints.list_attribute_terms(
                    attribute_id=aid,
                    description="List all product types/visuals",
                )]
            break
    return []


# ─── Attribute filters ───

def _build_filter_by_attribute(e, page, user_message: str = "") -> list:
    attr_filters = resolve_attr_filters(e.attributes)

    # Deduplicate tags that duplicate an attribute value
    attr_value_tokens = set()
    for v in e.attributes.values():
        attr_value_tokens |= {t for t in re.split(r'[\s\-_"/]+', v.lower()) if len(t) >= 2}
    deduped_tag_slugs = [
        slug for slug in (e.tag_slugs or [])
        if not {t for t in slug.split("-") if len(t) >= 2} <= attr_value_tokens
    ]

    attr_label = next(iter(e.attributes.keys()), "attribute")
    attr_value = next(iter(e.attributes.values()), "")

    actual_search = e.product_name or e.search_term
    if not actual_search and not deduped_tag_slugs and not e.target_category_slugs and not attr_filters and not e.product_id and not e.attr_tag_or_pairs:
        actual_search = user_message

    return [build_advanced_filter_call(
        tags=deduped_tag_slugs or None,
        attributes=attr_filters or None,
        or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
        page=page,
        in_stock=e.in_stock,
        categories=e.target_category_slugs,
        description=f"Filter by {attr_label}: {attr_value}",
        search_term=actual_search,
        product_id=e.product_id,
        **_common_exclusion_kwargs(e),
    )]


# ─── Variations ───

# AFTER
def _extract_resolved_attr_values(attributes: dict) -> list:
    tokens = []
    for key, val in attributes.items():
        key_norm = re.sub(r'[^a-z]', '', key.lower())   # e.g. 'colors2' → 'colors', 'visual' → 'visual'
        if isinstance(val, (list, tuple, set)):
            for v in val:
                v_norm = str(v).strip().lower()
                if v_norm and v_norm not in key_norm and key_norm not in v_norm:
                    tokens.append(v_norm)
        else:
            raw = re.sub(r'\s+(?:and|&)\s+', ',', str(val), flags=re.IGNORECASE)
            for t in raw.split(","):
                t_norm = t.strip().lower()
                if t_norm and t_norm not in key_norm and key_norm not in t_norm:
                    tokens.append(t_norm)
    return tokens


def _build_product_variations(e, page) -> list:
    attr_filters = resolve_attr_filters(e.attributes)

    # Build the filter call as before
    call = build_advanced_filter_call(
        product_id=e.product_id,
        search_term=e.product_name if not e.product_id else None,
        attributes=attr_filters or None,
        or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
        tags=list(e.tag_slugs) if e.tag_slugs else None,
        categories=e.target_category_slugs,
        excluded_tags=list(e.excluded_tags) if e.excluded_tags else None,
        excluded_categories=list(e.excluded_categories) if e.excluded_categories else None,
        excluded_attributes=e.excluded_attributes if hasattr(e, 'excluded_attributes') else None,
        tag_operator=e.tag_operator,
        page=page, in_stock=e.in_stock,
        description=f"Get specific variations for '{e.product_name or 'Series'}'",
    )

    # ── Inject resolved_attr_values so build_variant_prompt can pre-filter ──
    # When the user said "white and gray", resolved_attr_values=["white","gray"]
    # allows the variant prompt builder to show only matching colour options
    # instead of the full set (e.g. 5 colours → 2 pre-filtered options).
    if e.attributes:
        resolved_values = _extract_resolved_attr_values(e.attributes)
        if resolved_values:
            call.body["resolved_attr_values"] = resolved_values
            logger.debug(
                f"_build_product_variations: injected resolved_attr_values={resolved_values}"
            )

    return [call]


# ─── Discounts ───

def _build_discount_inquiry(e, page) -> list:
    return [endpoints.list_products_on_sale(
        page=page,
        per_page=DEFAULT_PER_PAGE,
        description="List products on sale",
    )]


def _build_bulk_discount(e, page) -> list:
    return [build_advanced_filter_call(
        search_term="bulk", page=page,
        description="Check for bulk discount products",
    )]


def _build_coupon_inquiry(e, page) -> list:
    return [endpoints.list_coupons(
        page=page,
        per_page=DEFAULT_PER_PAGE,
        description="List available coupon codes",
    )]


def _build_save_for_later(e, page) -> list:
    return [endpoints.fetch_wishlist(
        customer_id="CURRENT_USER",
        description="Get customer wishlist",
    )]

def _build_order_tracking(e, page, customer_id=None, role=None) -> list:
    if role in CUSTOM_ORDER_ROLES:
        return [endpoints.fetch_order(
            order_id=e.order_id,
            description=f"Get order #{e.order_id} details",
        )]

    # ── non-CS users with a specific order_id should also fetch directly ──
    if getattr(e, "order_id", None):
        return [endpoints.fetch_order(
            order_id=e.order_id,
            description=f"Get order #{e.order_id} details",
        )]

    return [endpoints.list_customer_orders(
        customer_id="CURRENT_USER_ID",
        page=page,
        per_page=5,
        description="List recent orders (no order ID provided)",
        requires_resolution=["customer_id"],
    )]
    
def _build_place_order(e, page) -> list:
    search_term = e.product_name or e.order_item_name
    attr_filters = resolve_attr_filters(e.attributes)
    if not (e.product_id or search_term):
        return []
    return [build_advanced_filter_call(
        product_id=e.product_id,
        search_term=search_term if not e.product_id else None,
        attributes=attr_filters or None,
        or_pairs=list(e.attr_tag_or_pairs) if e.attr_tag_or_pairs else None,
        page=1,
        description=f"Find product '{search_term}' for order placement",
    )]


def _build_update_customer(e, page, customer_id: Optional[int] = None) -> list:
    if not customer_id:
        logger.warning("api_builder: UPDATE_CUSTOMER intent but no customer_id resolved")
        return []
    payload = {}
    for field_key, value in (e.customer_updates or {}).items():
        if field_key not in ("role", "email", "password"):
            payload[field_key] = value
    if e.billing_updates:
        payload["billing"] = dict(e.billing_updates)
    if e.shipping_updates:
        payload["shipping"] = dict(e.shipping_updates)
    if not payload:
        logger.warning("api_builder: UPDATE_CUSTOMER payload empty after field filtering")
        return []
    logger.debug(f"api_builder: UPDATE_CUSTOMER | customer_id={customer_id} | keys={list(payload.keys())}")
    return [endpoints.update_customer(
        customer_id=customer_id,
        payload=payload,
        description=f"Update customer id={customer_id} | fields={list(payload.keys())}",
    )]
    
def _build_checkout(e, page) -> list:
    """
    Build a WooCommerce order creation call from the in-memory cart.
    cart_items is stamped into entities by chat.py before this is called.
    """
    if not e.cart_items:
        logger.warning("api_builder: CHECKOUT called but entities.cart_items is empty")
        return []

    line_items = [
        {
            "product_id": item["product_id"],
            **({"variation_id": item["variation_id"]} if item.get("variation_id") else {}),
            "quantity": item["qty"],
        }
        for item in e.cart_items
    ]

    shipping = (
        e.shipping_updates
        or {k: v for k, v in e.billing_updates.items()}
        or {}
    )

    payload = {
        "payment_method": "cod",           # default; frontend can override
        "payment_method_title": "Cash on Delivery",
        "set_paid": False,
        "customer_id": "CURRENT_USER_ID",  # resolved by _resolve_user_placeholders
        "line_items": line_items,
    }

    if shipping:
        payload["shipping"] = shipping
        payload["billing"] = shipping      # mirror shipping to billing if not split

    logger.debug(f"api_builder: _build_checkout | {len(line_items)} line items")

    return [endpoints.create_order(
        payload=payload,
        description=f"Create order for {len(line_items)} cart item(s)",
        requires_resolution=["customer_id"],
    )]


# ─── Fallback ───

def _build_fallback(e, page, intent, user_message: str = "") -> list:
    search = e.product_name or e.search_term or next(iter(e.attributes.values()), None)
    if search or e.product_id:
        logger.warning(f"api_builder: No calls for intent={intent.value} — fallback | search={search!r} | product_id={e.product_id}")
        return [build_advanced_filter_call(
            product_id=e.product_id,
            search_term=search if not e.product_id else None,
            page=page,
            description=f"Fallback search: '{search or e.product_id}'",
        )]
    logger.warning(
        f"api_builder: No calls for intent={intent.value} and NO search terms. "
        "Bypassing API call to trigger empty result handling."
    )
    return []