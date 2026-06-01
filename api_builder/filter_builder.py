"""
api_builder/filter_builder.py — Builds the WooAPICall for the custom
advanced product filter endpoint.

This is the core engine that converts tags, categories, attributes,
exclusions, OR pairs, and price ranges into a single POST body.
"""

import json
from typing import List, Optional

from models import WooAPICall
from app_config import CUSTOM_API_BASE_URL, DEFAULT_PER_PAGE
from chat_logger import get_logger

from api_builder.query_tree import (
    make_condition,
    make_or_group,
    make_price_condition,
    make_stock_condition,
    serialize_query,
    merge_cross_taxonomy_overlaps,
)
from api_builder.or_pairs import build_or_pair_conditions
from api_builder.store_helpers import (
    loader,
    attr_slug_for_label,
    get_attribute_term_slug,
)
from store_loader.config import ECOMMERCE_BACKEND

logger = get_logger("miraq_chat")
CUSTOM_API_BASE = CUSTOM_API_BASE_URL


def _group_categories(cat_slugs: list) -> dict:
    """
    Group category slugs by their parent category.
    Siblings under the same parent become a single IN (OR within group).
    Categories under different parents become separate AND conditions.
    """
    l = loader()
    groups = {}

    for slug in cat_slugs:
        parent_key = slug  # default: each slug is its own group

        if l and l.category_by_key:
            cat_obj = l.category_by_key.get(slug)
            if cat_obj:
                parent_id = (cat_obj.backend_ref or {}).get("parent_id", 0)
                parent_key = str(parent_id) if parent_id else slug

        groups.setdefault(parent_key, []).append(slug)

    return groups

def build_advanced_filter_call(
    tags=None, categories=None, attributes=None,
    excluded_tags=None, excluded_categories=None, excluded_attributes=None,
    tag_operator="AND",
    or_pairs=None,
    page=1, per_page=DEFAULT_PER_PAGE, description="",
    min_price=None, max_price=None, search_term=None,
    product_id=None, requires_resolution=None, in_stock=None,
    variation_page=None,
) -> WooAPICall:

    conditions = []

    # ── 1. OR pairs FIRST — so we know which tags/cats are already covered ──
    or_conditions, covered_tags, covered_cats = build_or_pair_conditions(or_pairs or [])
    conditions.extend(or_conditions)

    # ── 2. Tags (include) — skip any already in an OR pair ──
    uncovered_tags = [t for t in (tags or []) if t not in covered_tags]
    if uncovered_tags:
        if tag_operator == "AND" and len(uncovered_tags) > 1:
            for slug in uncovered_tags:
                conditions.append(make_condition("product_tag", [slug], "IN"))
        else:
            conditions.append(make_condition("product_tag", uncovered_tags, "IN"))

    # ── 3. Tags (exclude) ──
    if excluded_tags:
        conditions.append(make_condition("product_tag", list(excluded_tags), "NOT IN"))

    # ── 4. Categories (include) — skip any already in an OR pair ──
    if categories:
        uncovered_cats = [c for c in categories if c not in covered_cats]
        if uncovered_cats:
            grouped = _group_categories(uncovered_cats)
            for slugs in grouped.values():
                conditions.append(make_condition("product_cat", slugs, "IN"))

    # ── 5. Categories (exclude) ──
    if excluded_categories:
        conditions.append(make_condition("product_cat", list(excluded_categories), "NOT IN"))

    # ── 6. Attributes (exclude) ──
    if excluded_attributes:
        for taxonomy, slug_list in excluded_attributes.items():
            if slug_list:
                conditions.append(make_condition(taxonomy, slug_list, "NOT IN"))

    # ── 7. Attributes (include) ──
    if attributes:
        conditions.extend(_build_attribute_conditions(attributes, loader()))

    # ── 8. Cross-taxonomy overlap merge ──
    conditions = merge_cross_taxonomy_overlaps(conditions)

    # ── 9. Price / stock — push as field_type nodes so serialize_query
    #       routes them into body["price"] / body["stock_status"], which both
    #       WooQueryExecutor and ShopifyQueryExecutor read from the body. ──
    if min_price is not None or max_price is not None:
        conditions.append(make_price_condition(min_price, max_price))

    if in_stock is True:
        conditions.append(make_stock_condition("instock"))
    elif in_stock is False:
        conditions.append(make_stock_condition("outofstock"))

    # ── Serialize ──
    body = serialize_query(conditions, page, per_page)

    # product_id: inject as body["ids"] so both WooQueryExecutor (which reads
    # body["ids"]) and ShopifyQueryExecutor (which checks body["ids"] for its
    # in-memory filter) can use it. When product_id is present, stock/filter
    # conditions are irrelevant — clear them to avoid cross-contamination.
    if product_id:
        body["ids"] = [product_id]
        body.pop("stock_status", None)
        body.pop("filters", None)

        if variation_page is not None and variation_page > 1:
            body["variation_page"] = variation_page

    elif search_term:
        if ECOMMERCE_BACKEND == "shopify":
            # Shopify executor does in-memory name/tag/variant matching — always
            # write the term. It cannot "return arbitrary products" because the
            # haystack search in _apply_body() is a strict substring filter.
            body["search"] = search_term.lower().strip()
        else:
            has_taxonomy_conditions = conditions and any(
                "field_type" not in c for c in conditions
            )
            if has_taxonomy_conditions:
                logger.info(
                    f"Ignored leftover search_term='{search_term}' — "
                    "taxonomy filters are present, relying on them."
                )
            else:
                logger.warning(
                    f"search_term='{search_term}' ignored AND no taxonomy "
                    "conditions present — query will return arbitrary products. "
                    "Caller should use WooCommerce text search instead."
                )

    logger.debug(f"api_builder: Advanced filter body: {json.dumps(body)}")
    
    if ECOMMERCE_BACKEND == "shopify":
        return WooAPICall(
            method="POST",
            endpoint="shopify-graphql",       # logical name, never fetched
            params={},
            body=body,
            description=description or "Shopify GraphQL product filter",
            surface="shopify_graphql",        # ← new routing key
            requires_resolution=requires_resolution or [],
        )

    return WooAPICall(
        method="POST",
        endpoint=f"{CUSTOM_API_BASE}/products-advanced-new",
        params={},
        body=body,
        description=description or "Advanced product filter",
        surface="custom_plugin",
        requires_resolution=requires_resolution or [],
    )


# ─── Private helpers ───

def _normalise_attr_value(raw_value) -> str:
    """
    Normalise an attribute value into a comma-joined string regardless of
    whether it arrived as a plain string, a list, or a string that uses
    ' and ' / ' & ' as conjunctions (e.g. "white and gray").

    All callers downstream already split on commas, so this single
    normalisation step is the only change needed to support multi-value
    color/attribute queries.
    """
    if isinstance(raw_value, (list, tuple, set)):
        return ",".join(str(v).strip() for v in raw_value if str(v).strip())

    raw = str(raw_value).strip()
    # Convert "white and gray", "white & gray" → "white,gray"
    import re as _re
    raw = _re.sub(r"\s+(?:and|&)\s+", ",", raw, flags=_re.IGNORECASE)
    return raw


def _build_attribute_conditions(attributes: dict, l) -> list:
    """Convert {taxonomy: value} into query conditions, grouping shared values with OR.

    Supports multi-value attributes expressed as:
      - a list:             ['white', 'gray']
      - comma-separated:    'white,gray'
      - conjunction string: 'white and gray'  /  'white & gray'

    Multiple values for the same taxonomy are emitted as a single IN (OR) condition
    so the query finds products matching *any* of the requested values.
    """
    value_groups: dict[str, list] = {}
    for taxonomy, terms_value in attributes.items():
        raw = _normalise_attr_value(terms_value)
        key = raw.lower().strip()
        value_groups.setdefault(key, []).append(taxonomy)

    conditions = []
    for val_key, taxonomies in value_groups.items():
        raw_terms = [t.strip() for t in val_key.split(",") if t.strip()]
        or_conditions = []
        for taxonomy in taxonomies:
            slug_list = []
            for raw_term in raw_terms:
                term_slug = get_attribute_term_slug(taxonomy, raw_term) if l else None
                if term_slug:
                    slug_list.append(term_slug)
                else:
                    slug_list.append(
                        raw_term.replace(" ", "-").replace('"', "").replace("'", "")
                    )
            if slug_list:
                or_conditions.append(make_condition(taxonomy, slug_list, "IN"))

        if len(or_conditions) == 1:
            conditions.append(or_conditions[0])
        elif len(or_conditions) > 1:
            conditions.append(make_or_group(or_conditions))

    return conditions