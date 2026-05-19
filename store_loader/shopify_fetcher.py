"""
store_loader/shopify_fetcher.py — Bulk catalog loader for Shopify.

Pulls the entire catalog (products, collections, tags, attributes/options)
via the Shopify Admin GraphQL API and returns the same dict shape
``fetcher.load_from_live_api()`` produces, so `lookup_builder.py` can
populate the neutral catalog indexes without caring which backend filled them.

Auth:
    Requires SHOPIFY_STORE_DOMAIN (e.g. "miraq-demo.myshopify.com") and
    SHOPIFY_ADMIN_TOKEN (Admin API access token from a custom app).

Pagination:
    Uses Shopify's Relay-style cursor pagination. We just fully drain
    every connection — store loader is a one-shot bulk job that runs at
    boot and on webhook refresh.
"""

import time
from typing import Dict, List, Optional

import requests

from chat_logger import get_logger

logger = get_logger("miraq_chat")

API_VERSION = "2024-10"
PAGE_SIZE = 100  # Shopify GraphQL connection max
TIMEOUT = 30
MAX_RETRIES = 3


# ══════════════════════════════════════════════════════════════
# GRAPHQL TRANSPORT
# ══════════════════════════════════════════════════════════════

def _gql(session, store_domain: str, admin_token: str, query: str,
         variables: Optional[dict] = None) -> dict:
    """Execute a GraphQL query against the Shopify Admin API with retries."""
    url = f"https://{store_domain}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": admin_token,
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"Shopify GraphQL errors: {data['errors']}")
            # Respect API throttling
            cost = (data.get("extensions", {}) or {}).get("cost", {})
            available = (cost.get("throttleStatus", {}) or {}).get("currentlyAvailable", 1000)
            if available < 200:
                time.sleep(0.5)
            return data["data"]
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"ShopifyFetcher: retrying GraphQL ({e})")
                time.sleep(2 ** attempt)
            else:
                logger.error(f"ShopifyFetcher: GraphQL failed after retries: {e}")
                raise


def _drain_connection(session, store_domain: str, admin_token: str,
                      query_template: str, root_field: str,
                      extra_variables: Optional[dict] = None) -> List[dict]:
    """Walk a Relay connection until ``hasNextPage`` is false; return all nodes."""
    nodes = []
    cursor = None
    extra = extra_variables or {}

    while True:
        variables = {"first": PAGE_SIZE, "after": cursor, **extra}
        data = _gql(session, store_domain, admin_token, query_template, variables)
        connection = data[root_field]
        for edge in connection.get("edges", []):
            nodes.append(edge["node"])
        page_info = connection.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return nodes


# ══════════════════════════════════════════════════════════════
# QUERIES
# ══════════════════════════════════════════════════════════════

_SHOP_QUERY = """
query Shop {
  shop {
    name
    currencyCode
  }
}
"""

_COLLECTIONS_QUERY = """
query Collections($first: Int!, $after: String) {
  collections(first: $first, after: $after) {
    edges {
      node {
        id
        handle
        title
        description
        productsCount { count }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_PRODUCTS_QUERY = """
query Products($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    edges {
      node {
        id
        handle
        title
        descriptionHtml
        productType
        vendor
        tags
        status
        totalInventory
        priceRangeV2 {
          minVariantPrice { amount currencyCode }
          maxVariantPrice { amount currencyCode }
        }
        options { id name values }
        collections(first: 50) {
          edges { node { id handle title } }
        }
        images(first: 10) {
          edges { node { url altText } }
        }
        variants(first: 100) {
          edges {
            node {
              id
              title
              sku
              price
              compareAtPrice
              availableForSale
              inventoryQuantity
              selectedOptions { name value }
              image { url }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


# ══════════════════════════════════════════════════════════════
# NORMALISATION
# ══════════════════════════════════════════════════════════════

def _slugify(s: str) -> str:
    """Best-effort slug for tags/options that have no native handle."""
    return (
        (s or "")
        .strip()
        .lower()
        .replace("'", "")
        .replace('"', "")
        .replace("/", "-")
        .replace(" ", "-")
    )


def _normalise_collection(c: dict, idx: int) -> dict:
    """Shopify collection → Woo-shaped category dict consumed by lookup_builder."""
    return {
        "id":          idx + 1,           # synthetic numeric id
        "name":        c.get("title", ""),
        "slug":        c.get("handle", ""),
        "description": c.get("description") or "",
        "parent":      0,                  # Shopify collections have no parent
        "count":       int((c.get("productsCount") or {}).get("count", 0)),
        "_shopify_gid": c.get("id"),       # preserve native id in backend_ref
    }


def _normalise_product(p: dict, idx: int) -> dict:
    """Shopify product → Woo-shaped product dict consumed by lookup_builder."""
    variants = [edge["node"] for edge in (p.get("variants") or {}).get("edges", [])]
    images   = [edge["node"] for edge in (p.get("images")   or {}).get("edges", [])]
    cat_edges = (p.get("collections") or {}).get("edges", [])

    # Variants → Woo-shaped variation dicts
    variation_dicts = []
    for v in variants:
        variation_dicts.append({
            "id":            v.get("id"),
            "sku":           v.get("sku") or "",
            "price":         v.get("price") or "",
            "regular_price": v.get("price") or "",
            "sale_price":    v.get("compareAtPrice") or "",
            "in_stock":      bool(v.get("availableForSale")),
            "stock_status":  "instock" if v.get("availableForSale") else "outofstock",
            "attributes": [
                {"name": opt["name"], "option": opt["value"]}
                for opt in (v.get("selectedOptions") or [])
            ],
            "image":         (v.get("image") or {}).get("url") or "",
            "_shopify_gid":  v.get("id"),
        })

    # Product attributes
    attribute_dicts = []
    for opt in (p.get("options") or []):
        attribute_dicts.append({
            "id":        0,
            "name":      opt.get("name", ""),
            "slug":      opt.get("name", ""),       # neutral key derived later
            "options":   opt.get("values", []),
            "variation": True,
            "visible":   True,
        })

    price_range = p.get("priceRangeV2") or {}
    min_price = ((price_range.get("minVariantPrice") or {}).get("amount") or "0")

    return {
        "id":            idx + 1,                  # synthetic numeric id
        "name":          p.get("title", ""),
        "slug":          p.get("handle", ""),
        "type":          "variable" if len(variants) > 1 else "simple",
        "status":        (p.get("status") or "active").lower(),
        "description":   p.get("descriptionHtml") or "",
        "short_description": "",
        "sku":           variants[0].get("sku") if variants else "",
        "price":         min_price,
        "regular_price": min_price,
        "sale_price":    "",
        "stock_status":  "instock" if (p.get("totalInventory") or 0) > 0 else "outofstock",
        "in_stock":      (p.get("totalInventory") or 0) > 0,
        "permalink":     f"https://{p.get('handle','')}",  # frontend rewrites
        "categories": [
            {
                "id":   i + 1,
                "name": e["node"].get("title", ""),
                "slug": e["node"].get("handle", ""),
            }
            for i, e in enumerate(cat_edges)
        ],
        "tags": [
            {"id": i + 1, "name": t, "slug": _slugify(t)}
            for i, t in enumerate(p.get("tags") or [])
        ],
        "attributes":  attribute_dicts,
        "variations":  variation_dicts,
        "images": [
            {"id": i + 1, "src": img.get("url", ""), "alt": img.get("altText") or ""}
            for i, img in enumerate(images)
        ],
        "related_ids": [],
        "_shopify_gid": p.get("id"),
        "vendor":       p.get("vendor") or "",
        "product_type": p.get("productType") or "",
    }


def _aggregate_attributes(products: List[dict]) -> List[dict]:
    """
    Build a Woo-shaped `all_attributes_raw` from product options.

    Shopify has no global "attribute taxonomy" concept — options are per
    product. We aggregate: every distinct option name across the catalog
    becomes one global attribute, with every distinct value becoming a term.
    """
    by_name: Dict[str, Dict[str, dict]] = {}

    for product in products:
        for opt in product.get("attributes", []):
            name = opt.get("name", "")
            if not name:
                continue
            slot = by_name.setdefault(name, {})
            for value in opt.get("options", []):
                if value not in slot:
                    slot[value] = {
                        "id":    len(slot) + 1,
                        "name":  value,
                        "slug":  _slugify(value),
                        "count": 0,
                    }
                slot[value]["count"] += 1

    aggregated = []
    for idx, (name, term_map) in enumerate(by_name.items()):
        aggregated.append({
            "attribute_id":    idx + 1,
            "attribute_name":  _slugify(name),
            "attribute_label": name,
            "type":            "select",
            "order_by":        "menu_order",
            "taxonomy":        _slugify(name),     # NOT "pa_…"; Shopify-native
            "terms":           list(term_map.values()),
        })

    return aggregated


def _aggregate_tags(products: List[dict]) -> List[dict]:
    """Build a global tag list from per-product tags."""
    seen: Dict[str, dict] = {}
    for product in products:
        for tag in product.get("tags", []):
            slug = tag.get("slug")
            if slug and slug not in seen:
                seen[slug] = {
                    "id":    len(seen) + 1,
                    "name":  tag.get("name", ""),
                    "slug":  slug,
                    "count": 0,
                }
            if slug:
                seen[slug]["count"] += 1
    return list(seen.values())


# ══════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════

def load_from_shopify(store_domain: str, admin_token: str) -> dict:
    """
    Fetch the entire Shopify catalog and return it in the same dict shape
    that `fetcher.load_from_live_api()` returns, so `lookup_builder.py`
    can build the neutral indexes without backend awareness.

    Returns:
        {
            "categories":         list[dict],    # from Shopify collections
            "tags":               list[dict],    # aggregated from products
            "products":           list[dict],
            "all_attributes_raw": list[dict],    # aggregated from product options
            "attribute_terms":    dict[int, list],
            "currency_symbol":    str,
            "expected_product_count": int,
        }
    """
    logger.info(f"ShopifyFetcher: 🛍️  Fetching catalog from {store_domain}")

    session = requests.Session()

    # 1. Shop / currency
    shop_data = _gql(session, store_domain, admin_token, _SHOP_QUERY)
    currency_code = (shop_data.get("shop") or {}).get("currencyCode", "USD")
    currency_symbol = {
        "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹",
        "CAD": "C$", "AUD": "A$", "JPY": "¥",
    }.get(currency_code, currency_code)

    # 2. Collections
    logger.info("ShopifyFetcher: fetching collections…")
    raw_collections = _drain_connection(
        session, store_domain, admin_token, _COLLECTIONS_QUERY, "collections"
    )
    categories = [_normalise_collection(c, i) for i, c in enumerate(raw_collections)]

    # 3. Products (with embedded variants, options, tags, collections)
    logger.info("ShopifyFetcher: fetching products…")
    raw_products = _drain_connection(
        session, store_domain, admin_token, _PRODUCTS_QUERY, "products"
    )
    products = [_normalise_product(p, i) for i, p in enumerate(raw_products)]

    # 4. Aggregate attributes + tags from products
    all_attributes_raw = _aggregate_attributes(products)
    tags = _aggregate_tags(products)

    attribute_terms = {
        int(attr["attribute_id"]): attr.get("terms", [])
        for attr in all_attributes_raw
    }

    logger.info(
        f"ShopifyFetcher: ✅ {len(products)} products, {len(categories)} collections, "
        f"{len(tags)} tags, {len(all_attributes_raw)} attribute groups"
    )

    return {
        "categories":         categories,
        "tags":               tags,
        "products":           products,
        "all_attributes_raw": all_attributes_raw,
        "attribute_terms":    attribute_terms,
        "currency_symbol":    currency_symbol,
        "expected_product_count": len(products),
    }