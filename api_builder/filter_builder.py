"""
api_builder/filter_builder.py — Builds the WooAPICall for the custom
advanced product filter endpoint.

This is the core engine that converts tags, categories, attributes,
exclusions, OR pairs, and price ranges into a single POST body.
"""

import json
from typing import List, Optional

from models import WooAPICall
from app_config import DEFAULT_PER_PAGE
from chat_logger import get_logger

from api_builder.query_tree import (
    make_condition,
    make_or_group,
    make_price_condition,
    make_stock_condition,
    make_search_condition,
    serialize_query,
    merge_cross_taxonomy_overlaps,
)

from api_builder.or_pairs import (
    build_or_pair_conditions
)

from api_builder.store_helpers import (
    loader,
    attr_slug_for_label,
    get_attribute_term_slug,
)

logger = get_logger("miraq_chat")

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
            cat_obj = l.resolve_category(slug)
            if cat_obj:
                parent_key = cat_obj.parent_key if cat_obj.parent_key else slug

        groups.setdefault(parent_key, []).append(slug)

    return groups

def build_advanced_filter_call(
    tags=None, categories=None, attributes=None,
    excluded_tags=None, excluded_categories=None, excluded_attributes=None,
    tag_operator="AND",
    or_pairs=None,          # now List[OrPair] instead of List[dict]
    page=1, per_page=DEFAULT_PER_PAGE, description="",
    min_price=None, max_price=None, search_term=None,
    product_id=None, requires_resolution=None, in_stock=None,
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
            # Group by parent slug for AND vs OR within same taxonomy
            grouped = _group_categories(uncovered_cats)
            for slugs in grouped.values():
                conditions.append(make_condition("product_cat", slugs, "IN"))

    # ── 5. Categories (exclude) ──
    if excluded_categories:
        conditions.append(make_condition("product_cat", list(excluded_categories), "NOT IN"))

    # ── 6. Attributes (exclude) ──
    if excluded_attributes:
        l = loader()
        for attr_key, slug_list in excluded_attributes.items():
            if slug_list:
                taxonomy = _resolve_attribute_taxonomy(attr_key, l)
                conditions.append(make_condition(taxonomy, slug_list, "NOT IN"))

    # ── 7. Attributes (include) ──
    if attributes:
        conditions.extend(_build_attribute_conditions(attributes, loader()))

    # ── 8. Cross-taxonomy overlap merge ──
    conditions = merge_cross_taxonomy_overlaps(conditions)

    # ── Prepend special-field leaves ──
    # These are routed by serialize_query to the right top-level body fields.
    if in_stock is True:
        conditions = [make_stock_condition("instock")] + conditions
    elif in_stock is False:
        conditions = [make_stock_condition("outofstock")] + conditions

    if min_price is not None or max_price is not None:
        conditions = [make_price_condition(min_price=min_price, max_price=max_price)] + conditions

    # ── Serialize ──
    body = serialize_query(conditions, page, per_page)

    if product_id:
        body["ids"] = [product_id]
        body.pop("stock_status", None)
        body.pop("filters", None)
    elif search_term:
        has_taxonomy_conditions = conditions and any("field_type" not in c for c in conditions)
        if has_taxonomy_conditions:
            logger.info(f"Ignored leftover search_term='{search_term}' — taxonomy filters are present, relying on them.")
        else:
            # This should not happen if callers are routing correctly.
            # A blank body with no conditions will return arbitrary products.
            logger.warning(
                f"search_term='{search_term}' ignored AND no taxonomy conditions present — "
                "query will return arbitrary products. Caller should use WooCommerce text search instead."
            )

    logger.debug(f"api_builder: Advanced filter body: {json.dumps(body)}")

    return WooAPICall(
        method="POST",
        endpoint="/products-advanced-new",
        params={},
        body=body,
        description=description or "Advanced product filter",
        surface="custom_plugin",
        requires_resolution=requires_resolution or [],
    )


# ─── Private helpers ───

def _build_attribute_conditions(attributes: dict, l) -> list:
    """Convert {attr_key: comma_terms} into query conditions, grouping shared values with OR."""
    value_groups: dict[str, list] = {}
    for attr_key, terms_value in attributes.items():
        raw = terms_value if isinstance(terms_value, str) else ",".join(terms_value)
        key = raw.lower().strip()
        value_groups.setdefault(key, []).append(attr_key)

    conditions = []
    for val_key, attr_keys in value_groups.items():
        raw_terms = [t.strip() for t in val_key.split(",") if t.strip()]
        or_conditions = []
        for attr_key in attr_keys:
            taxonomy = _resolve_attribute_taxonomy(attr_key, l)
            slug_list = []
            for raw_term in raw_terms:
                term_slug = None
                if l:
                    term = l.resolve_attribute_term(attr_key, raw_term)
                    if term:
                        term_slug = term.backend_ref.get("slug")
                    if not term_slug:
                        term_slug = get_attribute_term_slug(taxonomy, raw_term)
                if term_slug:
                    slug_list.append(term_slug)
                else:
                    slug_list.append(raw_term.replace(" ", "-").replace('"', '').replace("'", ""))
            if slug_list:
                or_conditions.append(make_condition(taxonomy, slug_list, "IN"))

        if len(or_conditions) == 1:
            conditions.append(or_conditions[0])
        elif len(or_conditions) > 1:
            conditions.append(make_or_group(or_conditions))

    return conditions


def _resolve_attribute_taxonomy(attr_key: str, l) -> str:
    if l:
        attribute = l.resolve_attribute(attr_key)
        if attribute:
            taxonomy = attribute.backend_ref.get("taxonomy")
            if taxonomy:
                return taxonomy
    logger.warning(
        f"Deprecated attribute filter key '{attr_key}' detected; expected neutral attr_key. "
        "Treating key as legacy taxonomy (example neutral key: 'color', not 'pa_color')."
    )
    return attr_key
