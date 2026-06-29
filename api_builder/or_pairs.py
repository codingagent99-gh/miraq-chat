"""
api_builder/or_pairs.py — Builds OR-group conditions from OrPair objects.

Single source of truth for:
  1. Resolving neutral attribute keys → Woo pa_* taxonomies
  2. Resolving neutral attr_term values → WooCommerce term slugs
  3. Producing the final OR-group condition node
  4. Collecting tag/category slugs that are "covered" by OR pairs
     (so the main builder can skip them from standalone conditions)
"""

import re
from typing import List, Set, Tuple
from models import OrPair
from api_builder.query_tree import make_condition, make_or_group
from api_builder.store_helpers import loader
from chat_logger import get_logger
logger = get_logger("miraq_chat")

def resolve_or_pair(pair: OrPair) -> OrPair:
    """
    Return a new OrPair with taxonomy and term slugs fully resolved.
    Does NOT mutate the original.
    """
    # ── Defensive coercion: catalog_parser builds attr_tag_or_pairs as plain
    # dicts; JSON round-trips can also produce dicts. Normalize before any
    # attribute access so we never crash on .attr_taxonomy etc.
    if isinstance(pair, dict):
        pair = OrPair(
            tag_slug=pair.get("tag_slug"),
            cat_slugs=list(pair.get("cat_slugs") or []),
            attr_key=pair.get("attr_key"),
            attr_taxonomy=pair.get("attr_taxonomy"),
            attr_term=pair.get("attr_term"),
        )

    attr_key = pair.attr_key or (pair.attr_taxonomy.removeprefix("pa_") if pair.attr_taxonomy else None)
    taxonomy = pair.attr_taxonomy or ""
    term = pair.attr_term or ""

    # Resolve human term → WooCommerce slug, trying multiple normalisations
    # because the catalog stores slugified forms (e.g. "12-x-24") that may not
    # directly index to the actual WooCommerce term slug (e.g. "12x24").
    term_slug = term.lower().replace(" ", "-") if term else ""
    l = loader()
    if l and attr_key:
        attr = l.resolve_attribute(attr_key)
        if attr:
            taxonomy = attr.backend_ref.get("taxonomy", "") or taxonomy

            if term:
                # If the term is a dimension slug (e.g. "12-x-24"), use the canonical
                # inch-mark form directly: '12"x24"'.
                # Reason: pa_sample-size and pa_tile-size share the same dimension
                # but have DIFFERENT term.name values ("12\" x 24\"" vs "12 x 24"),
                # so using term.name would give different attr_term values → two AND
                # conditions. The constructed inch_form is the same for both taxonomies
                # → same attr_term → one OR group → matches the working query body.
                # The PHP endpoint passes it to WP_Tax_Query with field='slug';
                # WordPress calls sanitize_title('12"x24"') → '12x24' → matches DB slug.
                _dim_m = re.match(r'^(\d+)[\-\s]*[xX×][\-\s]*(\d+)$', term.strip())
                resolved_term = None
                if _dim_m:
                    candidates = [f'{_dim_m.group(1)}x{_dim_m.group(2)}', term]
                else:
                    candidates = [term, term.replace("-", " "), re.sub(r"[\s\-]+", "", term)]
                for candidate in filter(None, candidates):
                        resolved_term = l.resolve_attribute_term(attr_key, candidate)
                        if resolved_term:
                            break

                if resolved_term:
                    term_slug = (
                        resolved_term.backend_ref.get("slug")
                        or resolved_term.key
                        or term_slug
                    )
                else:
                    sample = [(t.key, t.name) for t in attr.terms[:8]]
                    logger.debug(
                        f"resolve_or_pair: resolution failed for attr='{attr_key}' "
                        f"term='{term}' | available terms (key/name): {sample}"
                    )

    return OrPair(
        tag_slug=pair.tag_slug,
        cat_slugs=list(pair.cat_slugs),
        attr_key=attr_key,
        attr_taxonomy=taxonomy,
        attr_term=term_slug,
    )

def summarize_or_pair_matches(products: list, pairs: list) -> dict:
    """
    For OR-pair searches, count how many of the already-fetched raw `products`
    satisfy each branch (category / tag / attribute) per attr_term.
    Computed locally — no extra API call.

    `products` must be the products-advanced-new raw shape: `categories` is
    [{"slug": ...}], `attributes` is {taxonomy: [term_names]}.

    Returns: {attr_term: {"category": int, "tag": int, "attribute": int}}
    (only keys for branches actually present in that pair are included)
    """
    if not pairs or not products:
        return {}

    summary: dict = {}
    for raw_pair in pairs:
        pair = resolve_or_pair(raw_pair)
        bucket = summary.setdefault(pair.attr_term or "", {})

        if pair.tag_slug:
            bucket["tag"] = bucket.get("tag", 0) + sum(
                1 for p in products
                if pair.tag_slug in {t.get("slug") for t in p.get("tags", []) if isinstance(t, dict)}
            )

        if pair.cat_slugs:
            cat_set = set(pair.cat_slugs)
            bucket["category"] = bucket.get("category", 0) + sum(
                1 for p in products
                if cat_set & {c.get("slug") for c in p.get("categories", []) if isinstance(c, dict)}
            )

        if pair.attr_taxonomy and pair.attr_term:
            term = pair.attr_term.lower()
            bucket["attribute"] = bucket.get("attribute", 0) + sum(
                1 for p in products
                if term in {
                    str(name).lower().replace(" ", "-")
                    for name in (p.get("attributes") or {}).get(pair.attr_taxonomy, [])
                }
            )

    return summary

def build_or_pair_conditions(pairs: List[OrPair]) -> Tuple[list, Set[str], Set[str]]:
    """
    Convert a list of OrPair into query-tree conditions.

    Pairs that share the same (tag_slug, attr_term) key are merged into a
    single OR group — e.g. {tag=None, pa_sample-size: 12x24} and
    {tag=None, pa_tile-size: 12x24} collapse into one OR condition:
    (pa_sample-size=12x24 OR pa_tile-size=12x24).
    Without merging they produce separate AND conditions which over-constrains
    the query and returns 0 results.

    Returns:
        conditions:   list of OR-group condition nodes to append
        covered_tags: set of tag slugs already inside an OR pair
        covered_cats: set of category slugs already inside an OR pair
    """
    from collections import OrderedDict

    conditions = []
    covered_tags: Set[str] = set()
    covered_cats: Set[str] = set()

    # Group pairs by (tag_slug, attr_term) — same key means same user value
    # expressed across multiple taxonomies; they belong in one OR group.
    grouped: OrderedDict = OrderedDict()
    for raw_pair in pairs:
        pair = resolve_or_pair(raw_pair)
        key = (pair.tag_slug or "", pair.attr_term or "")
        grouped.setdefault(key, []).append(pair)

    for (tag_slug, attr_term), group in grouped.items():
        or_conds = []

        # Tag branch (shared across the group)
        if tag_slug:
            or_conds.append(make_condition("product_tag", [tag_slug], "IN"))
            covered_tags.add(tag_slug)

        # Category branches — deduplicated across all pairs in the group,
        # combined into ONE multi-term leaf (not one leaf per slug) so the
        # breakdown counts as a single "Category" branch with per-slug
        # sub-counts, instead of N separate same-label branches.
        seen_cats: Set[str] = set()
        for pair in group:
            for slug in (pair.cat_slugs or []):
                seen_cats.add(slug)
                covered_cats.add(slug)
        if seen_cats:
            or_conds.append(make_condition("product_cat", sorted(seen_cats), "IN"))

        # Attribute branches — one per unique taxonomy in the group
        seen_taxonomies: Set[str] = set()
        for pair in group:
            if pair.attr_taxonomy and pair.attr_term and pair.attr_taxonomy not in seen_taxonomies:
                or_conds.append(make_condition(pair.attr_taxonomy, [pair.attr_term], "IN"))
                seen_taxonomies.add(pair.attr_taxonomy)

        if len(or_conds) >= 2:
            conditions.append(make_or_group(or_conds))
        elif len(or_conds) == 1:
            conditions.append(or_conds[0])

    logger.debug(f"[or_pairs_trace] grouped_keys={list(grouped.keys())} | conditions_returned={conditions}")

    return conditions, covered_tags, covered_cats