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
    make_sort_condition,
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

    No ancestor/descendant pruning is performed here — every originally
    extracted category slug is kept. An ancestor and its descendant always
    end up in different parent-groups below (their parent_key_str can never
    match), so they're combined via AND downstream. merge_cross_taxonomy_overlaps
    (query_tree.py) is what actually folds an ancestor category into a shared
    OR-group with same-named attributes/tags when appropriate — that's the
    intended mechanism for letting a descendant-tagged product (e.g. tagged
    only with a child category) satisfy the ancestor via subtree inclusion,
    without this function silently dropping anything itself.
    """
    l = loader()
    slug_list = list(cat_slugs)
    groups = {}

    if not l or not l.category_by_key:
        for slug in slug_list:
            groups.setdefault(slug, []).append(slug)
        return groups

    effective = slug_list

    for slug in effective:
        cat_obj = l.category_by_key.get(slug)
        parent_id = (cat_obj.backend_ref or {}).get("parent_id", 0) if cat_obj else 0
        parent_key_str = str(parent_id) if parent_id else slug
        groups.setdefault(parent_key_str, []).append(slug)

    return groups

def build_advanced_filter_call(
    tags=None, categories=None, attributes=None, category_groups=None,
    excluded_tags=None, excluded_categories=None, excluded_attributes=None,
    tag_operator="AND",
    or_pairs=None,
    page=1, per_page=DEFAULT_PER_PAGE, description="",
    min_price=None, max_price=None, search_term=None,
    product_id=None, requires_resolution=None, in_stock=None,
    variation_page=None, sort_by=None,
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
    # Categories (include) moved below — see step 8.5.
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
    
    # ── 8.5. Categories (include) — appended AFTER overlap merge, deliberately
    #         bypassing it. merge_cross_taxonomy_overlaps' same-taxonomy
    #         consolidation (built to dedupe catalog-duplicate slugs within
    #         ONE concept, e.g. duplicate pa_tile-size rows) would otherwise
    #         blindly re-merge two INTENTIONALLY separate category_groups
    #         conditions sharing the "product_cat" taxonomy string back into
    #         one OR'd list — undoing the AND semantics category_groups
    #         exists to provide. Genuine category/attribute overlaps are
    #         already resolved upstream into OR-pairs by
    #         consolidation.py's _resolve_category_attribute_overlap before
    #         entities ever reach this function, so nothing legitimate is
    #         lost by skipping this step for categories. ──
    if category_groups:
        for group in category_groups:
            uncovered = [c for c in group if c not in covered_cats]
            if uncovered:
                conditions.append(make_condition("product_cat", sorted(uncovered), "IN"))
    elif categories:
        uncovered_cats = [c for c in categories if c not in covered_cats]
        if uncovered_cats:
            grouped = _group_categories(uncovered_cats)
            for slugs in grouped.values():
                conditions.append(make_condition("product_cat", slugs, "IN"))

    # ── 9. Price / stock — push as field_type nodes so serialize_query
    #       routes them into body["price"] / body["stock_status"], which both
    #       WooQueryExecutor and ShopifyQueryExecutor read from the body. ──
    if min_price is not None or max_price is not None:
        conditions.append(make_price_condition(min_price, max_price))

    if in_stock is True:
        conditions.append(make_stock_condition("instock"))
    elif in_stock is False:
        conditions.append(make_stock_condition("outofstock"))

    # ── 9.5. Sort order ──
    if sort_by:
        conditions.append(make_sort_condition(sort_by))

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
                _term = search_term.lower().strip()
                _stop = {'all', 'products', 'items', 'everything', 'anything',
                        'show', 'me', 'give', 'find', 'search', 'list', 'get', 'browse'}
                _meaningful = bool(set(_term.split()) - _stop)
                if _meaningful:
                    body["search"] = _term
                    logger.info(
                        f"filter_builder: No taxonomy conditions — writing "
                        f"search_term='{search_term}' to body['search'] as text-search fallback."
                    )
                else:
                    logger.info(
                        f"filter_builder: Ignoring stop-word search_term='{search_term}' "
                        "— returning unfiltered product list."
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

    Two layers of OR grouping:
      1. Multiple values for the same taxonomy → single IN condition
         (e.g. colors-2: "black,blue" → pa_colors-2 IN [black, blue]).
      2. A value that resolves under MORE than one taxonomy (e.g. "blue"
         under both pa_color and pa_colors-2 — discovered from actual
         resolved data, not a hardcoded taxonomy pair list) gets pulled
         into its own small OR-of-taxonomies node. Any taxonomy whose
         entire term list is already covered by such a node is dropped
         as redundant; one with leftover unique terms (e.g. colors-2
         also has "black") stays as a full standalone condition.
    """
    taxonomy_terms: dict[str, list] = {}
    for taxonomy, terms_value in attributes.items():
        raw = _normalise_attr_value(terms_value)
        raw_terms = [t.strip() for t in raw.split(",") if t.strip()]
        slug_list = []
        for raw_term in raw_terms:
            term_slug = get_attribute_term_slug(taxonomy, raw_term) if l else None
            resolved = term_slug or raw_term.replace(" ", "-").replace('"', "").replace("'", "")
            if resolved not in slug_list:
                slug_list.append(resolved)
        if slug_list:
            taxonomy_terms[taxonomy] = slug_list

    if not taxonomy_terms:
        return []

    # Map each resolved slug -> which taxonomies it appears under.
    slug_to_taxonomies: dict[str, set] = {}
    for taxonomy, slugs in taxonomy_terms.items():
        for slug in slugs:
            slug_to_taxonomies.setdefault(slug, set()).add(taxonomy)

    # Union-find: cluster taxonomies that share at least one resolved slug.
    parent = {t: t for t in taxonomy_terms}
    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for taxes in slug_to_taxonomies.values():
        taxes = list(taxes)
        for i in range(1, len(taxes)):
            union(taxes[0], taxes[i])

    clusters: dict[str, list] = {}
    for t in taxonomy_terms:
        clusters.setdefault(find(t), []).append(t)

    conditions = []
    for cluster_taxonomies in clusters.values():
        if len(cluster_taxonomies) == 1:
            tax = cluster_taxonomies[0]
            conditions.append(make_condition(tax, taxonomy_terms[tax], "IN"))
            continue

        shared_slugs = {
            slug for slug, taxes in slug_to_taxonomies.items()
            if len(taxes & set(cluster_taxonomies)) > 1
        }

        group_conditions = []
        for slug in shared_slugs:
            taxes_for_slug = sorted(slug_to_taxonomies[slug] & set(cluster_taxonomies))
            inner = [make_condition(t, [slug], "IN") for t in taxes_for_slug]
            group_conditions.append(make_or_group(inner) if len(inner) > 1 else inner[0])

        for tax in cluster_taxonomies:
            if not set(taxonomy_terms[tax]) <= shared_slugs:
                group_conditions.append(make_condition(tax, taxonomy_terms[tax], "IN"))

        conditions.append(
            group_conditions[0] if len(group_conditions) == 1
            else make_or_group(group_conditions)
        )

    return conditions