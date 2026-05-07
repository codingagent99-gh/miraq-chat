"""
api_builder/or_pairs.py — Builds OR-group conditions from OrPair objects.

Single source of truth for:
  1. Resolving neutral attribute keys → Woo pa_* taxonomies
  2. Resolving neutral attr_term values → WooCommerce term slugs
  3. Producing the final OR-group condition node
  4. Collecting tag/category slugs that are "covered" by OR pairs
     (so the main builder can skip them from standalone conditions)
"""

from typing import List, Set, Tuple
from models import OrPair
from api_builder.query_tree import make_condition, make_or_group
from api_builder.store_helpers import loader

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

    # Resolve human term → WooCommerce slug
    term_slug = term.lower().replace(" ", "-") if term else ""
    l = loader()
    if l and attr_key:
        attr = l.resolve_attribute(attr_key)
        if attr:
            taxonomy = attr.backend_ref.get("taxonomy", "") or taxonomy

            if term:
                resolved_term = l.resolve_attribute_term(attr_key, term)
                if resolved_term:
                    term_slug = (
                        resolved_term.backend_ref.get("slug")
                        or resolved_term.key
                        or term_slug
                    )

    return OrPair(
        tag_slug=pair.tag_slug,
        cat_slugs=list(pair.cat_slugs),
        attr_key=attr_key,
        attr_taxonomy=taxonomy,
        attr_term=term_slug,
    )


def build_or_pair_conditions(pairs: List[OrPair]) -> Tuple[list, Set[str], Set[str]]:
    """
    Convert a list of OrPair into query-tree conditions.

    Returns:
        conditions:     list of OR-group condition nodes to append
        covered_tags:   set of tag slugs already inside an OR pair (skip in standalone tag conditions)
        covered_cats:   set of category slugs already inside an OR pair (skip in standalone cat conditions)
    """
    conditions = []
    covered_tags: Set[str] = set()
    covered_cats: Set[str] = set()

    for raw_pair in pairs:
        pair = resolve_or_pair(raw_pair)

        or_conds = []
        if pair.tag_slug:
            or_conds.append(make_condition("product_tag", [pair.tag_slug], "IN"))
            covered_tags.add(pair.tag_slug)
        if pair.cat_slugs:
            or_conds.append(make_condition("product_cat", pair.cat_slugs, "IN"))
            covered_cats.update(pair.cat_slugs)
        if pair.attr_taxonomy and pair.attr_term:
            or_conds.append(make_condition(pair.attr_taxonomy, [pair.attr_term], "IN"))

        if len(or_conds) >= 2:
            conditions.append(make_or_group(or_conds))
        elif len(or_conds) == 1:
            # Degraded to a single branch — still valid, just not OR
            conditions.append(or_conds[0])

    return conditions, covered_tags, covered_cats
