"""
api_builder/query_tree.py — Low-level query tree nodes for the custom
WooCommerce advanced filter API.

Responsible for:
  - Creating condition nodes (IN / NOT IN)
  - Creating OR groups
  - Serializing the tree into the POST body format
  - Cross-taxonomy overlap merging
"""

from typing import List, Optional


# ─── Node constructors ───

def make_condition(taxonomy: str, terms: List[str], operator: str = "IN") -> dict:
    """Build a single filter condition node."""
    return {
        "taxonomy": taxonomy,
        "field": "slug",
        "terms": terms,
        "operator": operator,
    }


def make_or_group(conditions: list) -> dict:
    """Wrap a list of conditions in a nested OR group node."""
    return {"relation": "OR", "conditions": conditions}


# ─── Serialization ───

def serialize_condition(condition: dict) -> dict:
    """Recursively serialize a single condition node."""
    if "conditions" in condition:
        return {
            "relation": condition["relation"],
            "conditions": [serialize_condition(sub) for sub in condition["conditions"]],
        }
    return {
        "taxonomy": condition["taxonomy"],
        "terms": condition["terms"],
        "operator": condition["operator"],
    }


def serialize_query(
    conditions: list,
    page: int,
    per_page: int,
    min_price: float = None,
    max_price: float = None,
) -> dict:
    """Serialize a list of condition nodes to the POST body format."""
    body = {"page": page, "per_page": per_page}

    if min_price is not None or max_price is not None:
        price = {}
        if min_price is not None:
            price["min"] = min_price
        if max_price is not None:
            price["max"] = max_price
        body["price"] = price

    if conditions:
        body["filters"] = {
            "relation": "AND",
            "conditions": [serialize_condition(c) for c in conditions],
        }

    return body


# ─── Cross-taxonomy overlap merger ───

def _normalize_term(t: str) -> str:
    """Normalize a term for overlap comparison (lowercase, strip trailing 's')."""
    t = str(t).lower().strip()
    if t.endswith('s') and not t.endswith('ss'):
        return t[:-1]
    return t


def merge_cross_taxonomy_overlaps(conditions: list) -> list:
    """
    Detect conditions across different taxonomies that target the same
    normalized term (e.g. 'mosaic' tag + 'mosaic' category) and wrap
    them in an OR group so either match satisfies the filter.
    """
    flattened_in = []
    other = []

    for cond in conditions:
        if cond.get("operator") == "IN" and "terms" in cond and cond["terms"]:
            flattened_in.append(cond)
        elif cond.get("relation") == "OR":
            # Only flatten OR groups where all sub-conditions share the same base term
            subs = cond.get("conditions", [])
            all_ins = all(
                sub.get("operator") == "IN" and sub.get("terms")
                for sub in subs
            )
            base_terms = {_normalize_term(sub["terms"][0]) for sub in subs} if all_ins else set()

            if all_ins and len(base_terms) == 1:
                flattened_in.extend(subs)
            else:
                other.append(cond)
        else:
            other.append(cond)

    # Group flattened IN conditions by their normalized first term
    term_groups: dict[str, list] = {}
    for cond in flattened_in:
        base = _normalize_term(cond["terms"][0])
        term_groups.setdefault(base, [])
        if cond not in term_groups[base]:
            term_groups[base].append(cond)

    final = list(other)
    for group in term_groups.values():
        if len(group) == 1:
            final.append(group[0])
        else:
            final.append(make_or_group(group))

    return final