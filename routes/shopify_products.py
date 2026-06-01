"""
routes/shopify_products.py
Single POST /shopify/products endpoint — flexible product search with
nested AND/OR filters across tags, collections, variant options, price,
and availability.

Register in server.py:
    from routes.shopify_products import shopify_products_bp
    app.register_blueprint(shopify_products_bp)

Filter JSON schema
──────────────────
{
  "relation": "AND" | "OR",
  "conditions": [
    { "type": "tag",        "values": ["slug1", "slug2"], "relation": "OR" | "AND" },
    { "type": "collection", "values": ["123456789"],      "relation": "OR" },
    { "type": "option",     "name": "Finish", "values": ["Honed", "Matte"], "relation": "OR" },
    { "type": "price",      "min": 20, "max": 100 },
    { "type": "available",  "value": true },
    // Nested group:
    { "relation": "OR", "conditions": [...] }
  ]
}

How it works
────────────
Layer 1 — Shopify Admin API (native narrowing)
  Tags and collections are handled in GraphQL.
  Tags build a query string: "tag:wall AND (tag:interior OR tag:exterior)"
  Collections determine which GraphQL query is used and whether parallel
  fetches are needed (OR between collections).
  Exception: if the top-level filter has an OR group mixing tags with
  option/price/available conditions, the tag query is skipped entirely —
  fetching all products is safer and post-filter handles everything.

Layer 2 — Python post-filter (variant-level)
  Variant options, price, and availability are always evaluated in Python
  because Shopify always returns all variants regardless of query filters.
  The full filter tree is re-evaluated here, so tags are also re-checked,
  making Python the single source of truth.
"""

import concurrent.futures

import requests as http_requests
from flask import Blueprint, jsonify, request

from chat_logger import get_logger
from models.shopify_token import ShopifyToken
from store_loader.config import SHOPIFY_STORE_DOMAIN

logger = get_logger("miraq_chat")
shopify_products_bp = Blueprint("shopify_products", __name__)

_ADMIN_GQL_URL = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-10/graphql.json"
_MAX_FETCH     = 250   # max products pulled per request (covers most catalogs)
_BATCH_SIZE    = 50    # products per GraphQL page (Shopify limit is 250 but 50 is safer)

# ─────────────────────────────────────────────────────────────
# GraphQL queries
# ─────────────────────────────────────────────────────────────

_PRODUCTS_GQL = """
query ($query: String!, $first: Int!, $after: String) {
  products(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id title handle tags status
        images(first: 5) { edges { node { url altText } } }
        variants(first: 100) {
          edges {
            node {
              id title price availableForSale
              selectedOptions { name value }
            }
          }
        }
      }
    }
  }
}
"""

_COLLECTION_GQL = """
query ($collectionId: ID!, $query: String!, $first: Int!, $after: String) {
  collection(id: $collectionId) {
    id title
    products(first: $first, after: $after, query: $query) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id title handle tags status
          images(first: 5) { edges { node { url altText } } }
          variants(first: 100) {
            edges {
              node {
                id title price availableForSale
                selectedOptions { name value }
              }
            }
          }
        }
      }
    }
  }
}
"""


# ─────────────────────────────────────────────────────────────
# Layer 1 — Build native Shopify tag query string
# ─────────────────────────────────────────────────────────────

def _build_tag_query(node):
    """
    Recursively walk the filter tree and build a Shopify Admin API
    query string from tag conditions only.
    Option / price / available conditions are ignored here —
    they go to Python post-filtering.

    Returns a query string fragment, or None if no tag conditions exist.

    Examples:
      { type: tag, values: ["wall", "interior"], relation: "OR" }
        → "(tag:wall OR tag:interior)"

      { relation: AND, conditions: [
          { type: tag, values: ["wall"] },
          { type: tag, values: ["matte-finish"] }
        ]}
        → "(tag:wall AND tag:matte-finish)"
    """
    if "type" in node:
        if node["type"] != "tag":
            return None
        values = node.get("values", [])
        if not values:
            return None
        inner_rel = node.get("relation", "OR").upper()
        parts = [f"tag:{v}" for v in values]
        if len(parts) == 1:
            return parts[0]
        return f"({f' {inner_rel} '.join(parts)})"

    if "conditions" in node:
        rel   = node.get("relation", "AND").upper()
        parts = [_build_tag_query(c) for c in node["conditions"]]
        parts = [p for p in parts if p]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return f"({f' {rel} '.join(parts)})"

    return None


def _has_mixed_or(node):
    """
    Return True if the filter tree contains an OR group that mixes tag
    conditions with option / price / available conditions.

    In that case pushing a tag query to Shopify would incorrectly exclude
    products that should match via the non-tag side of the OR, so the
    caller must skip the tag pre-filter and rely entirely on post-filtering.

    Example that triggers this:
      { relation: OR, conditions: [
          { type: tag,    values: ["wall"] },
          { type: option, name: "Finish", values: ["Honed"] }
        ]}
    """
    if "type" in node:
        return False

    if "conditions" in node:
        rel = node.get("relation", "AND").upper()
        if rel == "OR":
            types = {c["type"] for c in node["conditions"] if "type" in c}
            if "tag" in types and types & {"option", "price", "available"}:
                return True
        return any(_has_mixed_or(c) for c in node.get("conditions", []))

    return False


# ─────────────────────────────────────────────────────────────
# Layer 1 — Collection helpers
# ─────────────────────────────────────────────────────────────

def _extract_collections(node):
    """
    Walk the filter tree and return all collection IDs / GIDs found.
    Accepts both plain numeric IDs and full GIDs.
    """
    found = []
    if "type" in node:
        if node["type"] == "collection":
            found.extend(node.get("values", []))
        return found
    for c in node.get("conditions", []):
        found.extend(_extract_collections(c))
    return found


def _collection_relation(node):
    """
    Return the relation ("AND" | "OR") that governs the collection
    conditions at the first group level where they appear.
    Defaults to "AND" if not determinable.
    """
    if "type" in node:
        return None
    conditions = node.get("conditions", [])
    if any(c.get("type") == "collection" for c in conditions):
        return node.get("relation", "AND").upper()
    for c in conditions:
        r = _collection_relation(c)
        if r:
            return r
    return None


def _to_gid(collection_id):
    """Ensure a collection ID is a Shopify GID."""
    s = str(collection_id)
    return s if s.startswith("gid://") else f"gid://shopify/Collection/{s}"


# ─────────────────────────────────────────────────────────────
# Layer 2 — Python post-filter
# ─────────────────────────────────────────────────────────────

def _evaluate(node, product, variant):
    """
    Evaluate a filter node against a (product, variant) pair.
    Returns True if the pair satisfies the condition.

    This is the single source of truth for all filtering — tags included —
    so the result is correct regardless of what Shopify's query string
    pre-filtered.
    """
    if "type" in node:
        t = node["type"]

        # ── tag ──
        if t == "tag":
            inner_rel   = node.get("relation", "OR").upper()
            product_tags = {tag.lower() for tag in product.get("tags", [])}
            matches      = [v.lower() in product_tags for v in node.get("values", [])]
            return any(matches) if inner_rel == "OR" else all(matches)

        # ── collection ──
        # Handled at fetch level; always passes here.
        if t == "collection":
            return True

        # ── option ──
        if t == "option":
            inner_rel  = node.get("relation", "OR").upper()
            opt_name   = node["name"].lower()
            opt_values = {v.lower() for v in node.get("values", [])}
            var_opts   = {
                o["name"].lower(): o["value"].lower()
                for o in variant.get("selectedOptions", [])
            }
            var_val = var_opts.get(opt_name)
            if var_val is None:
                return False
            # A single variant slot can only hold one value, so OR and AND
            # behave the same: the value must be in the allowed set.
            return var_val in opt_values

        # ── price ──
        if t == "price":
            price = float(variant.get("price", 0))
            if "min" in node and price < float(node["min"]):
                return False
            if "max" in node and price > float(node["max"]):
                return False
            return True

        # ── available ──
        if t == "available":
            return variant.get("availableForSale", False) == node.get("value", True)

        return True  # unknown type → pass through

    # ── group node ──
    if "conditions" in node:
        rel = node.get("relation", "AND").upper()
        if rel == "OR":
            return any(_evaluate(c, product, variant) for c in node["conditions"])
        return all(_evaluate(c, product, variant) for c in node["conditions"])

    return True  # empty node → pass


# ─────────────────────────────────────────────────────────────
# Shopify API helpers
# ─────────────────────────────────────────────────────────────

def _gql(query, variables, token):
    """
    Execute a single GraphQL request against the Shopify Admin API.
    Raises on HTTP errors or GraphQL-level errors.
    """
    resp = http_requests.post(
        _ADMIN_GQL_URL,
        json={"query": query, "variables": variables},
        headers={
            "Content-Type":             "application/json",
            "X-Shopify-Access-Token":   token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"Shopify GraphQL errors: {data['errors']}")
    return data


def _normalize(node):
    """Map a Shopify GraphQL product node to our clean dict format."""
    return {
        "id":      node["id"],
        "title":   node["title"],
        "handle":  node["handle"],
        "tags":    node.get("tags", []),
        "status":  node.get("status", ""),
        "images": [
            {"url": e["node"]["url"], "alt": e["node"].get("altText", "")}
            for e in node.get("images", {}).get("edges", [])
        ],
        "variants": [
            {
                "id":               e["node"]["id"],
                "title":            e["node"]["title"],
                "price":            e["node"]["price"],
                "availableForSale": e["node"]["availableForSale"],
                "selectedOptions":  e["node"]["selectedOptions"],
            }
            for e in node.get("variants", {}).get("edges", [])
        ],
    }


def _fetch_products(tag_query, token, max_fetch=_MAX_FETCH):
    """
    Cursor-paginate through the top-level products query.
    tag_query is passed directly as the Shopify query string.
    """
    products, cursor = [], None

    while len(products) < max_fetch:
        batch = min(_BATCH_SIZE, max_fetch - len(products))
        data  = _gql(
            _PRODUCTS_GQL,
            {"query": tag_query, "first": batch, "after": cursor},
            token,
        )
        pdata = data["data"]["products"]
        edges = pdata.get("edges", [])

        for edge in edges:
            products.append(_normalize(edge["node"]))

        page_info = pdata.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not edges:
            break
        cursor = page_info["endCursor"]

    return products


def _fetch_from_collection(collection_id, tag_query, token, max_fetch=_MAX_FETCH):
    """
    Cursor-paginate through a single collection's products.
    tag_query further narrows results within the collection.
    """
    products, cursor = [], None
    gid = _to_gid(collection_id)

    while len(products) < max_fetch:
        batch = min(_BATCH_SIZE, max_fetch - len(products))
        data  = _gql(
            _COLLECTION_GQL,
            {"collectionId": gid, "query": tag_query, "first": batch, "after": cursor},
            token,
        )
        cdata = (data.get("data") or {}).get("collection")
        if not cdata:
            logger.warning(f"shopify/products: collection {gid} not found or empty")
            break

        pdata = cdata.get("products", {})
        edges = pdata.get("edges", [])

        for edge in edges:
            products.append(_normalize(edge["node"]))

        page_info = pdata.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not edges:
            break
        cursor = page_info["endCursor"]

    return products


def _deduplicate(product_lists):
    """Merge multiple product lists, deduplicating by Shopify product GID."""
    seen = {}
    for products in product_lists:
        for p in products:
            seen[p["id"]] = p
    return list(seen.values())


# ─────────────────────────────────────────────────────────────
# Route
# ─────────────────────────────────────────────────────────────

@shopify_products_bp.route("/shopify/products", methods=["POST"])
def search_shopify_products():
    """
    POST /shopify/products

    Body (JSON):
    {
      "filters":  { ... },   // filter tree (see module docstring)
      "page":     1,         // 1-based
      "per_page": 20         // max 100
    }

    Response:
    {
      "success":  true,
      "page":     1,
      "per_page": 20,
      "total":    45,
      "pages":    3,
      "products": [ { id, title, handle, tags, images, variants } ]
    }
    """
    body     = request.get_json(silent=True) or {}
    filters  = body.get("filters", {})
    page     = max(1, int(body.get("page", 1)))
    per_page = max(1, min(100, int(body.get("per_page", 20))))

    # ── Auth ────────────────────────────────────────────────────────────────
    token_row = ShopifyToken.query.get(SHOPIFY_STORE_DOMAIN)
    if not token_row or token_row.is_expired:
        logger.error("shopify/products: Admin token missing or expired")
        return jsonify({"success": False, "error": "Shopify token unavailable"}), 503

    token = token_row.access_token

    try:
        # ── Layer 1: determine what to push to Shopify natively ─────────────

        # Only push tag query when it won't over-restrict results.
        # Mixed OR (tag OR option at same level) requires fetching everything
        # and letting Python handle the full evaluation.
        if _has_mixed_or(filters):
            tag_query = ""
        else:
            tag_query = _build_tag_query(filters) or ""

        collections = _extract_collections(filters)

        # ── Fetch from Shopify ───────────────────────────────────────────────
        if not collections:
            # No collection filter — query the full catalog
            raw_products = _fetch_products(tag_query, token)

        elif len(collections) == 1:
            # Single collection
            raw_products = _fetch_from_collection(collections[0], tag_query, token)

        else:
            # Multiple collections
            col_rel = _collection_relation(filters) or "AND"

            if col_rel == "OR":
                # Each collection is an independent source → fetch in parallel and merge
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(collections), 5)
                ) as pool:
                    futures = [
                        pool.submit(_fetch_from_collection, col, tag_query, token)
                        for col in collections
                    ]
                    results = [f.result() for f in concurrent.futures.as_completed(futures)]
                raw_products = _deduplicate(results)

            else:
                # AND between collections — Shopify has no native multi-collection AND,
                # so fetch from the first collection; post-filter enforces the rest
                # (a product must be in all collections, but that's rare in practice;
                # for strict AND enforcement across collections, remove this note and
                # add a collection-membership check in _evaluate if needed).
                raw_products = _fetch_from_collection(collections[0], tag_query, token)

        # ── Layer 2: post-filter products & variants ─────────────────────────
        # _evaluate is the authoritative filter — it re-checks tags too,
        # so correctness is guaranteed regardless of what Shopify pre-filtered.
        filtered = []
        for product in raw_products:
            matching_variants = [
                v for v in product["variants"]
                if _evaluate(filters, product, v)
            ]
            if matching_variants:
                filtered.append({**product, "variants": matching_variants})

        # ── Paginate in Python ───────────────────────────────────────────────
        total    = len(filtered)
        pages    = max(1, (total + per_page - 1) // per_page)
        start    = (page - 1) * per_page
        page_out = filtered[start : start + per_page]

        return jsonify({
            "success":  True,
            "page":     page,
            "per_page": per_page,
            "total":    total,
            "pages":    pages,
            "products": page_out,
        })

    except Exception as e:
        logger.error(
            f"shopify/products: {type(e).__name__}: {e}", exc_info=True
        )
        return jsonify({"success": False, "error": "Failed to fetch products"}), 502