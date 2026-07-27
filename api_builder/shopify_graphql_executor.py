"""
api_builder/shopify_graphql_executor.py — GraphQL-backed product search for Shopify.

Replaces ShopifyQueryExecutor (in-memory) as the chat pipeline's product
search engine when ECOMMERCE_BACKEND=shopify.

Translation layer
─────────────────
filter_builder.py serialises the classifier's entities into a query body:

    {
        "filters": {
            "relation": "AND",
            "conditions": [
                {"taxonomy": "product_tag",  "terms": ["matte"],   "operator": "IN"},
                {"taxonomy": "finish",        "terms": ["honed"],   "operator": "IN"},
                {"taxonomy": "product_cat",   "terms": ["wall-tiles"], "operator": "IN"},
            ]
        },
        "search":       "marble",
        "price":        {"min": 20, "max": 100},
        "stock_status": "instock",
        "ids":          [42],          # product_id fast-path
        "page":         1,
        "per_page":     20,
    }

This executor translates that body into the two-layer filter approach:

    Layer 1 — Shopify Admin GraphQL (native narrowing)
        • Tags    → query string  "tag:matte AND tag:honed"
        • Collections → collection.products() query, parallel for OR
        • Mixed OR (tag + option at same level) → skip tag pre-filter
        • price / stock / search → Python post-filter only

    Layer 2 — Python post-filter (variant-level, authoritative)
        • Variant selectedOptions  (e.g. Finish=Honed)
        • Price range
        • Stock (availableForSale)
        • Tags (re-verified — Python is source of truth)
        • Free-text (name + description + tag names + variant option values)

Collection slug → GID resolution
─────────────────────────────────
lookup_builder drops _shopify_gid when building category_by_key, so we
walk store_loader.categories (the raw list) to map slug → GID.
"""

import concurrent.futures
from typing import Optional

import requests as http_requests

from chat_logger import get_logger
from models.shopify_token import ShopifyToken
from store_loader.config import SHOPIFY_STORE_DOMAIN

logger = get_logger("miraq_chat")

_MAX_FETCH  = 250
_BATCH_SIZE = 50


# ══════════════════════════════════════════════════════════════
# GraphQL queries  (identical to the ones in shopify_products.py)
# ══════════════════════════════════════════════════════════════

_PRODUCTS_GQL = """
query ($query: String!, $first: Int!, $after: String) {
  products(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id title handle tags status
        images(first: 5) { edges { node { url altText } } }
        collections(first: 50) { edges { node { id handle title } } }
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
query ($collectionId: ID!, $first: Int!, $after: String) {
  collection(id: $collectionId) {
    id title
    products(first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id title handle tags status
          images(first: 5) { edges { node { url altText } } }
          collections(first: 50) { edges { node { id handle title } } }
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


# ══════════════════════════════════════════════════════════════
# Helpers (mirror shopify_products.py exactly)
# ══════════════════════════════════════════════════════════════

def _gql(query, variables, token):
    resp = http_requests.post(
        f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-10/graphql.json",
        json={"query": query, "variables": variables},
        headers={
            "Content-Type":           "application/json",
            "X-Shopify-Access-Token": token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"Shopify GraphQL errors: {data['errors']}")
    return data


def _normalize(node: dict) -> dict:
    """Shopify GraphQL product node → clean dict (same shape as shopify_products.py)."""
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
        "collections": [
            {"id": e["node"]["id"], "handle": e["node"]["handle"], "title": e["node"]["title"]}
            for e in node.get("collections", {}).get("edges", [])
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


def _to_gid(collection_id: str) -> str:
    s = str(collection_id)
    return s if s.startswith("gid://") else f"gid://shopify/Collection/{s}"


def _fetch_products(tag_query: str, token: str, max_fetch: int = _MAX_FETCH) -> list:
    products, cursor = [], None
    while len(products) < max_fetch:
        batch = min(_BATCH_SIZE, max_fetch - len(products))
        data  = _gql(_PRODUCTS_GQL, {"query": tag_query, "first": batch, "after": cursor}, token)
        pdata = data["data"]["products"]
        for edge in pdata.get("edges", []):
            products.append(_normalize(edge["node"]))
        pi = pdata.get("pageInfo", {})
        if not pi.get("hasNextPage") or not pdata.get("edges"):
            break
        cursor = pi["endCursor"]
    return products

def _fetch_from_collection(collection_id: str, tag_query: str, token: str,
                            max_fetch: int = _MAX_FETCH) -> list:
    products, cursor = [], None
    gid = _to_gid(collection_id)
    while len(products) < max_fetch:
        batch = min(_BATCH_SIZE, max_fetch - len(products))
        data  = _gql(_COLLECTION_GQL,
                     {"collectionId": gid, "first": batch, "after": cursor},
                     token)
        cdata = (data.get("data") or {}).get("collection")
        if not cdata:
            logger.warning(f"ShopifyGraphQLExecutor: collection {gid} not found or empty")
            break
        pdata = cdata.get("products", {})
        for edge in pdata.get("edges", []):
            products.append(_normalize(edge["node"]))
        pi = pdata.get("pageInfo", {})
        if not pi.get("hasNextPage") or not pdata.get("edges"):
            break
        cursor = pi["endCursor"]
    return products


def _loader_product_to_gql_shape(p: dict) -> dict:
    """StoreLoader (Woo-shaped) product → the _normalize() GraphQL shape.

    Lets the Layer-2 post-filter and _to_woo_shape run unchanged over the
    in-memory catalog when a query has no native Layer-1 narrowing (no tag
    query, no collection). The loader holds the ENTIRE catalog (fully drained
    at boot by shopify_fetcher), so this path has no _MAX_FETCH ceiling.

    Loader products carry a synthetic numeric ``id`` with the GID in
    ``_shopify_gid``; the GraphQL shape (and everything downstream, including
    GID-sniffing) expects the GID as ``id``, so map it here. Variation dicts
    already use GIDs as ``id``.
    """
    return {
        "id":     p.get("_shopify_gid") or p.get("id"),
        "title":  p.get("name", ""),
        "handle": p.get("slug", ""),
        "tags":   [t.get("name", "") for t in (p.get("tags") or []) if isinstance(t, dict)],
        "status": (p.get("status") or "active").upper(),
        "images": [
            {"url": img.get("src", ""), "alt": img.get("alt", "")}
            for img in (p.get("images") or []) if isinstance(img, dict)
        ],
        "collections": [
            {"id": c.get("id"), "handle": c.get("slug", ""), "title": c.get("name", "")}
            for c in (p.get("categories") or []) if isinstance(c, dict)
        ],
        "variants": [
            {
                "id":               v.get("_shopify_gid") or v.get("id"),
                "title":            "",
                "price":            v.get("price", ""),
                "availableForSale": bool(v.get("in_stock")),
                "selectedOptions": [
                    {"name": a.get("name", ""), "value": a.get("option", "")}
                    for a in (v.get("attributes") or []) if isinstance(a, dict)
                ],
            }
            for v in (p.get("variations") or []) if isinstance(v, dict)
        ],
    }


def _deduplicate(product_lists: list) -> list:
    seen = {}
    for products in product_lists:
        for p in products:
            seen[p["id"]] = p
    return list(seen.values())


# ══════════════════════════════════════════════════════════════
# Body → filter tree translation
# ══════════════════════════════════════════════════════════════

def _build_shopify_filter_tree(conditions: list) -> dict:
    """
    Recursively convert filter_builder conditions into the shopify_products.py
    filter tree shape that _evaluate() understands.

    filter_builder condition shapes:
        {"taxonomy": "product_tag",  "terms": ["slug1"], "operator": "IN"}
        {"taxonomy": "product_cat",  "terms": ["slug1"], "operator": "IN"}
        {"taxonomy": "finish",       "terms": ["honed"], "operator": "IN"}
        {"relation": "OR",  "conditions": [...]}   ← OR group
        {"field_type": "price", ...}               ← handled outside filters
        {"field_type": "stock_status", ...}        ← handled outside filters

    Target shapes for _evaluate():
        {"type": "tag",        "values": [...], "relation": "OR"}
        {"type": "collection", "values": [...], "relation": "OR"}
        {"type": "option",     "name": "Finish", "values": [...], "relation": "OR"}
        {"type": "price",      "min": N, "max": N}
        {"type": "available",  "value": True}
        {"relation": "AND"|"OR", "conditions": [...]}   ← group
    """
    if not conditions:
        return {}

    converted = [_convert_condition(c) for c in conditions]
    converted = [c for c in converted if c]  # drop None (field_type nodes)

    if not converted:
        return {}
    if len(converted) == 1:
        return converted[0]
    return {"relation": "AND", "conditions": converted}


def _convert_condition(c: dict) -> Optional[dict]:
    """Convert a single filter_builder condition node to _evaluate() shape."""
    # ── group node ──
    if "relation" in c and "conditions" in c:
        rel = c["relation"].upper()
        subs = [_convert_condition(sub) for sub in c["conditions"]]
        subs = [s for s in subs if s]
        if not subs:
            return None
        if len(subs) == 1:
            return subs[0]
        return {"relation": rel, "conditions": subs}

    # ── field_type nodes (price / stock) — handled at body level, skip here ──
    if c.get("field_type"):
        return None

    taxonomy = c.get("taxonomy", "")
    terms    = c.get("terms") or []
    operator = c.get("operator", "IN").upper()

    if not terms:
        return None

    # NOT IN → same typed node with negate=True. _evaluate() inverts the
    # membership result; _build_tag_query / _extract_collections /
    # _collection_relation all skip negated nodes so exclusions are enforced
    # ONLY in the Python post-filter (never at the Shopify fetch layer).
    # Previously these conditions were silently dropped, so refinement
    # exclusions ("not glossy") returned wrong results without any error.
    negate = (operator == "NOT IN")

    inner_rel = "OR"  # filter_builder IN conditions are always OR within the term list

    # ── tag ──
    if taxonomy == "product_tag":
        node = {"type": "tag", "values": terms, "relation": inner_rel}
    # ── collection ──
    elif taxonomy == "product_cat":
        node = {"type": "collection", "values": terms, "relation": inner_rel}
    # ── variant option (everything else) ──
    # taxonomy is the attribute key, e.g. "finish", "size", "pa_color" → strip pa_
    else:
        attr_name = taxonomy.removeprefix("pa_").replace("-", " ").title()
        node = {"type": "option", "name": attr_name, "values": terms, "relation": inner_rel}

    if negate:
        node["negate"] = True
    return node


# ══════════════════════════════════════════════════════════════
# Layer 1 helpers  (ported from shopify_products.py verbatim)
# ══════════════════════════════════════════════════════════════

def _build_tag_query(node: dict) -> Optional[str]:
    if "type" in node:
        if node["type"] != "tag":
            return None
        if node.get("negate"):
            return None   # exclusions are enforced in the Python post-filter only
        values = node.get("values", [])
        if not values:
            return None
        rel   = node.get("relation", "OR").upper()
        parts = [f"tag:{v}" for v in values]
        return parts[0] if len(parts) == 1 else f"({f' {rel} '.join(parts)})"

    if "conditions" in node:
        rel   = node.get("relation", "AND").upper()
        parts = [_build_tag_query(c) for c in node["conditions"]]
        parts = [p for p in parts if p]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else f"({f' {rel} '.join(parts)})"

    return None


def _has_mixed_or(node: dict) -> bool:
    if "type" in node:
        return False
    if "conditions" in node:
        if node.get("relation", "AND").upper() == "OR":
            types = {c["type"] for c in node["conditions"] if "type" in c}
            if "tag" in types and types & {"option", "price", "available"}:
                return True
        return any(_has_mixed_or(c) for c in node.get("conditions", []))
    return False


def _extract_collections(node: dict) -> list:
    found = []
    if "type" in node:
        if node["type"] == "collection" and not node.get("negate"):
            found.extend(node.get("values", []))
        return found
    for c in node.get("conditions", []):
        found.extend(_extract_collections(c))
    return found


def _collection_relation(node: dict) -> Optional[str]:
    if "type" in node:
        return None
    conditions = node.get("conditions", [])
    if any(c.get("type") == "collection" and not c.get("negate") for c in conditions):
        return node.get("relation", "AND").upper()
    for c in conditions:
        r = _collection_relation(c)
        if r:
            return r
    return None


# ══════════════════════════════════════════════════════════════
# Layer 2 — Python post-filter  (ported from shopify_products.py verbatim)
# ══════════════════════════════════════════════════════════════

def _evaluate(node: dict, product: dict, variant: dict) -> bool:
    if "type" in node:
        t      = node["type"]
        negate = bool(node.get("negate"))

        def _norm(s: str) -> str:
            # Normalise both sides: lowercase + replace hyphens with spaces
            # so "glossy-finish" matches "Glossy Finish" from Shopify GraphQL
            return s.lower().replace("-", " ").replace("_", " ").strip()

        if t == "tag":
            rel          = node.get("relation", "OR").upper()
            product_tags = {_norm(tag) for tag in product.get("tags", [])}
            matches      = [_norm(v) in product_tags for v in node.get("values", [])]
            result       = any(matches) if rel == "OR" else all(matches)

        elif t == "collection":
            if not negate:
                result = True  # positive filter enforced at fetch level
            else:
                # Negated collections are NOT enforced at fetch (see
                # _extract_collections), so membership must be evaluated here.
                # result = "is a member"; the shared inversion below turns it
                # into the exclusion.
                wanted = {_norm(v) for v in node.get("values", [])}
                have   = set()
                for c in product.get("collections", []):
                    have.add(_norm(c.get("handle", "")))
                    have.add(_norm(c.get("title", "")))
                result = bool(wanted & have)

        elif t == "option":
            rel       = node.get("relation", "OR").upper()
            opt_name  = node["name"].lower()
            opt_vals  = {v.lower() for v in node.get("values", [])}
            var_opts  = {
                o["name"].lower(): o["value"].lower()
                for o in variant.get("selectedOptions", [])
            }
            var_val = var_opts.get(opt_name)
            result  = var_val in opt_vals if var_val is not None else False

        elif t == "price":
            price  = float(variant.get("price", 0))
            result = True
            if "min" in node and price < float(node["min"]):
                result = False
            if "max" in node and price > float(node["max"]):
                result = False

        elif t == "available":
            result = variant.get("availableForSale", False) == node.get("value", True)

        else:
            result = True

        return (not result) if negate else result

    if "conditions" in node:
        rel = node.get("relation", "AND").upper()
        if rel == "OR":
            return any(_evaluate(c, product, variant) for c in node["conditions"])
        return all(_evaluate(c, product, variant) for c in node["conditions"])

    return True


def _post_filter(raw_products: list, filter_tree: dict,
                 search_f: str, price_f: dict, stock_f: str) -> list:
    """
    Apply Layer 2 filtering to raw GraphQL products.
    Returns products with only matching variants retained.
    """
    results = []
    for product in raw_products:

        # ── free-text ──
        if search_f:
            tag_text = " ".join(
                t.lower() for t in product.get("tags", [])
            )
            variation_text = " ".join(
                o["value"].lower()
                for v in product.get("variants", [])
                for o in v.get("selectedOptions", [])
            )
            haystack = " ".join(filter(None, [
                product.get("title", "").lower(),
                tag_text,
                variation_text,
            ]))
            if search_f not in haystack:
                continue

        # ── per-variant filter ──
        matching_variants = []
        for variant in product.get("variants", []):
            # price
            if price_f:
                price = float(variant.get("price", 0))
                mn, mx = price_f.get("min"), price_f.get("max")
                if mn is not None and price < float(mn):
                    continue
                if mx is not None and price > float(mx):
                    continue
            # stock
            if stock_f:
                avail = variant.get("availableForSale", False)
                if stock_f == "instock" and not avail:
                    continue
                if stock_f == "outofstock" and avail:
                    continue
            # filter tree (tags, options, collections)
            if filter_tree and not _evaluate(filter_tree, product, variant):
                continue
            matching_variants.append(variant)

        if matching_variants:
            results.append({**product, "variants": matching_variants})

    return results


# ══════════════════════════════════════════════════════════════
# Result normalisation  (GraphQL → Woo-shaped dicts chat.py expects)
# ══════════════════════════════════════════════════════════════

def _to_woo_shape(gql_product: dict) -> dict:
    """
    Convert a _normalize()-shaped GraphQL product into the Woo-shaped dict
    that format_product() / format_variation() in formatters.py expects.

    Mirrors shopify_fetcher._normalise_product() but works on the lighter
    GraphQL node (no descriptionHtml, no priceRangeV2, etc.).
    """
    variants = gql_product.get("variants", [])
    images   = gql_product.get("images", [])

    variation_dicts = []
    for v in variants:
        variation_dicts.append({
            "id":            v["id"],
            "price":         v.get("price", ""),
            "regular_price": v.get("price", ""),
            "sale_price":    "",
            "in_stock":      v.get("availableForSale", False),
            "stock_status":  "instock" if v.get("availableForSale") else "outofstock",
            "attributes": [
                {"name": o["name"], "option": o["value"]}
                for o in v.get("selectedOptions", [])
            ],
            "_shopify_gid": v["id"],
        })

    any_in_stock = any(v.get("availableForSale", False) for v in variants)
    min_price    = min((v.get("price", "0") for v in variants), default="0",
                       key=lambda p: float(p or 0))

    return {
        "id":            gql_product["id"],   # Shopify GID string
        "_shopify_gid":  gql_product["id"],
        "name":          gql_product["title"],
        "slug":          gql_product["handle"],
        "type":          "variable" if len(variants) > 1 else "simple",
        "status":        gql_product.get("status", "active").lower(),
        "description":   "",
        "short_description": "",
        "price":         min_price,
        "regular_price": min_price,
        "sale_price":    "",
        "in_stock":      any_in_stock,
        "stock_status":  "instock" if any_in_stock else "outofstock",
        "permalink":     f"https://{SHOPIFY_STORE_DOMAIN}/products/{gql_product['handle']}",
        "categories": [
            {"id": i + 1, "name": c["title"], "slug": c["handle"]}
            for i, c in enumerate(gql_product.get("collections", []))
        ],
        "tags": [
            {"id": i + 1, "name": t, "slug": t.lower().replace(" ", "-")}
            for i, t in enumerate(gql_product.get("tags", []))
        ],
        "attributes":  [],     # not returned; variant options are in variations
        "variations":  variation_dicts,
        "images": [
            {"id": i + 1, "src": img.get("url", ""), "alt": img.get("alt", "")}
            for i, img in enumerate(images)
        ],
        "related_ids": [],
    }


# ══════════════════════════════════════════════════════════════
# Executor
# ══════════════════════════════════════════════════════════════

class ShopifyGraphQLExecutor:
    """
    Drop-in replacement for ShopifyQueryExecutor.

    Accepts the same execute_from_body(body) interface and returns the
    identical response shape:

        {
            "products": [woo-shaped dicts],
            "page":     int,
            "per_page": int,
            "total":    int,
            "pages":    int,
            "_raw":     dict,
        }

    Usage in chat.py (_execute_api_calls):

        executor = ShopifyGraphQLExecutor(get_store_loader())
        result   = executor.execute_from_body(call.body)
    """

    def __init__(self, store_loader):
        self._loader = store_loader

    # ── public ───────────────────────────────────────────────

    def execute_from_body(self, body: dict) -> dict:
        import time
        t0       = time.time()
        page     = body.get("page", 1)
        per_page = body.get("per_page", 4)

        logger.info(
            f"[ShopifyGQL] execute_from_body | page={page} per_page={per_page} "
            f"search={body.get('search')!r} stock={body.get('stock_status')!r} "
            f"price={body.get('price')} ids={body.get('ids')}"
        )

        token = self._get_token()

        # ── product_id fast-path ─────────────────────────────
        ids = body.get("ids")
        if ids:
            logger.info(f"[ShopifyGQL] fast-path: fetching by ids={ids}")
            result = self._fetch_by_ids(ids, page, per_page, token)
            logger.info(
                f"[ShopifyGQL] fast-path done | found={result['total']} "
                f"elapsed={round(time.time()-t0,2)}s"
            )
            return result

        # ── translate body → filter tree ─────────────────────
        raw_conditions = (body.get("filters") or {}).get("conditions") or []
        filter_tree    = _build_shopify_filter_tree(raw_conditions)

        logger.debug(f"[ShopifyGQL] filter_tree={filter_tree}")

        search_f = (body.get("search") or "").lower().strip()
        price_f  = body.get("price") or {}
        stock_f  = body.get("stock_status") or ""

        # ── Layer 1: Shopify native fetch ─────────────────────
        mixed_or = _has_mixed_or(filter_tree)
        if mixed_or:
            tag_query = ""
            logger.info("[ShopifyGQL] Layer1: mixed OR detected — skipping tag pre-filter")
        else:
            tag_query = _build_tag_query(filter_tree) or ""
            if tag_query:
                logger.info(f"[ShopifyGQL] Layer1: tag_query={tag_query!r}")

        collections = _extract_collections(filter_tree)
        logger.info(
            f"[ShopifyGQL] Layer1: collections={collections} "
            f"tag_query={tag_query!r}"
        )

        if not collections:
            loader_products = getattr(self._loader, "products", None) if self._loader else None
            if not tag_query and loader_products:
                # No native narrowing at all → a live fetch would be a
                # full-catalog scan truncated at _MAX_FETCH (250). The loader
                # already holds the COMPLETE catalog in memory (refreshed by
                # RefreshScheduler), so filter that instead: no ceiling, no
                # API cost. Freshness is bounded by the loader refresh cycle;
                # tag/collection-narrowed queries below remain fully live.
                logger.info(
                    f"[ShopifyGQL] Layer1: no native narrowing — using in-memory "
                    f"catalog ({len(loader_products)} products, no fetch cap)"
                )
                raw = [_loader_product_to_gql_shape(p) for p in loader_products]
            else:
                logger.info("[ShopifyGQL] Layer1: no collection filter — querying full catalog")
                raw = _fetch_products(tag_query, token)

        elif len(collections) == 1:
            gid = self._slug_to_gid(collections[0])
            logger.info(f"[ShopifyGQL] Layer1: single collection slug={collections[0]!r} gid={gid!r}")
            raw = _fetch_from_collection(gid, tag_query, token)

        else:
            col_rel = _collection_relation(filter_tree) or "AND"
            logger.info(
                f"[ShopifyGQL] Layer1: multi-collection count={len(collections)} "
                f"relation={col_rel}"
            )
            if col_rel == "OR":
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(collections), 5)
                ) as pool:
                    futures = [
                        pool.submit(_fetch_from_collection,
                                    self._slug_to_gid(col), tag_query, token)
                        for col in collections
                    ]
                    raw = _deduplicate(
                        [f.result() for f in concurrent.futures.as_completed(futures)]
                    )
                logger.info(f"[ShopifyGQL] Layer1: parallel fetch + dedup → {len(raw)} products")
            else:
                gid = self._slug_to_gid(collections[0])
                logger.info(
                    f"[ShopifyGQL] Layer1: AND collections — fetching first "
                    f"slug={collections[0]!r} gid={gid!r}, post-filter enforces rest"
                )
                raw = _fetch_from_collection(gid, tag_query, token)

        logger.info(f"[ShopifyGQL] Layer1 done | raw_products={len(raw)}")

        # ── Layer 2: Python post-filter ───────────────────────
        filtered = _post_filter(raw, filter_tree, search_f, price_f, stock_f)
        logger.info(
            f"[ShopifyGQL] Layer2 done | after_filter={len(filtered)} "
            f"(dropped {len(raw)-len(filtered)})"
        )

        # ── Convert to Woo-shaped dicts ───────────────────────
        woo_products = [_to_woo_shape(p) for p in filtered]

        # ── Paginate ──────────────────────────────────────────
        total  = len(woo_products)
        pages  = max(1, -(-total // per_page)) if total else 0
        start  = (page - 1) * per_page
        sliced = woo_products[start: start + per_page]
        self._attach_loader_details(sliced)

        logger.info(
            f"[ShopifyGQL] result | total={total} pages={pages} "
            f"page={page} returning={len(sliced)} "
            f"elapsed={round(time.time()-t0,2)}s"
        )

        return {
            "products": sliced,
            "page":     page,
            "per_page": per_page,
            "total":    total,
            "pages":    pages,
            "_raw":     {"products": sliced},
        }

    # ── private ──────────────────────────────────────────────

    def _get_token(self) -> str:
        token_row = ShopifyToken.query.get(SHOPIFY_STORE_DOMAIN)
        if not token_row or token_row.is_expired:
            raise RuntimeError("Shopify Admin token missing or expired")
        return token_row.access_token

    def _attach_loader_details(self, woo_products: list) -> list:
        """Fill description/short_description from the store-loader copy.

        The live GraphQL queries deliberately omit descriptionHtml (kept light
        for search), so _to_woo_shape leaves them empty. The loader's bulk
        fetch DOES load descriptions — join them in for the returned page so
        product-detail answers aren't blank. In-place; returns the list.
        """
        loader_products = getattr(self._loader, "products", None) if self._loader else None
        if not (woo_products and loader_products):
            return woo_products
        by_gid = {
            p.get("_shopify_gid"): p
            for p in loader_products if p.get("_shopify_gid")
        }
        for wp in woo_products:
            src = by_gid.get(wp.get("_shopify_gid") or wp.get("id"))
            if not src:
                continue
            if not wp.get("description"):
                wp["description"] = src.get("description", "")
            if not wp.get("short_description"):
                wp["short_description"] = src.get("short_description", "")
        return woo_products

    def _slug_to_gid(self, slug: str) -> str:
        """
        Map a collection slug to its Shopify GID using the raw categories list.

        lookup_builder builds category_by_key as CatalogCategory objects and
        drops _shopify_gid, so we walk store_loader.categories (the raw list
        from shopify_fetcher._normalise_collection) which still carries it.
        """
        for cat in (self._loader.categories or []):
            if cat.get("slug") == slug:
                gid = cat.get("_shopify_gid")
                if gid:
                    return gid
        # Fallback: treat slug as numeric id or pass-through GID
        logger.warning(f"ShopifyGraphQLExecutor: no GID found for slug '{slug}', using slug as-is")
        return _to_gid(slug)

    def _fetch_by_ids(self, ids: list, page: int, per_page: int, token: str) -> dict:
        """
        Fetch specific products by Shopify GID or synthetic numeric id.
        Resolves numeric ids → GIDs via store_loader.categories raw list.
        """
        gids = []
        id_set = {str(i) for i in ids}
        for p in (self._loader.products or []):
            if (str(p.get("id", "")) in id_set
                    or str(p.get("_shopify_gid", "")) in id_set):
                gid = p.get("_shopify_gid")
                if gid:
                    gids.append(gid)

        if not gids:
            return {"products": [], "page": page, "per_page": per_page,
                    "total": 0, "pages": 0, "_raw": {}}

        # Build a tag query using the GID filter Shopify supports
        # Shopify doesn't support id: IN queries in the product query string,
        # so we fetch each product via a single-product products query using
        # "id:<numeric>" query syntax (strip gid:// prefix for query string).
        raw = []
        for gid in gids:
            numeric = gid.split("/")[-1]
            result  = _fetch_products(f"id:{numeric}", token, max_fetch=1)
            raw.extend(result)

        woo_products = [_to_woo_shape(p) for p in raw]
        total  = len(woo_products)
        pages  = max(1, -(-total // per_page)) if total else 0
        start  = (page - 1) * per_page
        sliced = woo_products[start: start + per_page]
        self._attach_loader_details(sliced)

        return {
            "products": sliced,
            "page":     page,
            "per_page": per_page,
            "total":    total,
            "pages":    pages,
            "_raw":     {"products": sliced},
        }