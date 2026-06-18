"""
api_builder/query_tree.py — Low-level query tree nodes for the custom
WooCommerce advanced filter API.

Responsible for:
  - Creating condition nodes (IN / NOT IN)
  - Creating OR groups
  - Creating special-field leaf nodes (price, stock, search)
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


def make_price_condition(min_price=None, max_price=None) -> dict:
    """A price-range leaf node."""
    return {"field_type": "price", "min": min_price, "max": max_price}


def make_stock_condition(stock_status: str) -> dict:
    """A stock-status leaf node."""
    return {"field_type": "stock_status", "value": stock_status}


def make_search_condition(search_term: str) -> dict:
    """A free-text search leaf node."""
    return {"field_type": "search", "value": search_term}


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
) -> dict:
    """Serialize a list of condition nodes to the POST body format.

    Conditions may include special field_type leaves:
      - {"field_type": "price", "min": ..., "max": ...}  → body["price"]
      - {"field_type": "stock_status", "value": ...}     → body["stock_status"]
      - {"field_type": "search", "value": ...}           → body["search"]
    All other conditions are routed into body["filters"].
    """
    body = {"page": page, "per_page": per_page}

    taxonomy_conditions = []
    for cond in conditions:
        ft = cond.get("field_type")
        if ft == "price":
            price = {}
            if cond.get("min") is not None:
                price["min"] = cond["min"]
            if cond.get("max") is not None:
                price["max"] = cond["max"]
            if price:
                body["price"] = price
        elif ft == "stock_status":
            body["stock_status"] = cond["value"]
        elif ft == "search":
            body["search"] = cond["value"]
        else:
            taxonomy_conditions.append(cond)

    if taxonomy_conditions:
        body["filters"] = {
            "relation": "AND",
            "conditions": [serialize_condition(c) for c in taxonomy_conditions],
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

def find_or_groups(node) -> list:
    """
    Recursively collect every OR-group's leaf conditions from a parsed
    filter tree (the body["filters"] structure, or any sub-node of it).
    Returns a list of branch-lists, e.g.:
        [[{"taxonomy": "product_cat", "terms": ["pavers"], ...},
          {"taxonomy": "pa_application", "terms": ["paver"], ...}], ...]
    """
    groups = []
    if isinstance(node, list):
        for n in node:
            groups.extend(find_or_groups(n))
        return groups
    if not isinstance(node, dict):
        return groups

    if node.get("relation") == "OR":
        subs = node.get("conditions", [])
        if subs and all("taxonomy" in s for s in subs):
            groups.append(subs)
        else:
            groups.extend(find_or_groups(subs))
    elif "conditions" in node:
        groups.extend(find_or_groups(node["conditions"]))

    return groups


def _branch_role(taxonomy: str) -> str:
    if taxonomy == "product_cat":
        return "category"
    if taxonomy == "product_tag":
        return "tag"
    if taxonomy.startswith("pa_"):
        return "attribute"
    return "other"


def count_or_group_matches(products: list, branch_conditions: list) -> list:
    """
    Per-branch product counts for one OR-group's leaf conditions, computed
    locally from already-fetched `products` (products-advanced-new raw shape).
    Returns: [{"taxonomy", "terms", "role", "count"}, ...]
    """
    results = []
    for cond in branch_conditions:
        taxonomy = cond.get("taxonomy", "")
        terms = {str(t).lower() for t in cond.get("terms", [])}
        role = _branch_role(taxonomy)

        if role == "category":
            count = sum(1 for p in products if terms & {
                c.get("slug", "").lower() for c in p.get("categories", []) if isinstance(c, dict)
            })
        elif role == "tag":
            count = sum(1 for p in products if terms & {
                t.get("slug", "").lower() for t in p.get("tags", []) if isinstance(t, dict)
            })
        elif role == "attribute":
            count = sum(1 for p in products if terms & {
                str(n).lower().replace(" ", "-")
                for n in (p.get("attributes") or {}).get(taxonomy, [])
            })
        else:
            count = None

        results.append({"taxonomy": taxonomy, "terms": cond.get("terms", []), "role": role, "count": count})
    return results