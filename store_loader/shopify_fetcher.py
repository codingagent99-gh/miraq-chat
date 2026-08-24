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

import hashlib
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

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


_GID_TAIL_RE = re.compile(r"/(\d+)(?:\?.*)?$")


def _gid_numeric(gid, fallback: Optional[int] = None) -> Optional[int]:
    """Extract the permanent numeric id from a Shopify GID.

    ``gid://shopify/Product/8123456789012`` → ``8123456789012``.

    This is the id Shopify itself uses everywhere — the admin URL, the Ajax
    Cart API, webhook payloads — so it is both globally unique and permanent.
    Returns ``fallback`` when the value is missing or not a GID, so a
    malformed record degrades to the old positional behaviour for that one
    entry instead of colliding on ``None``.
    """
    if gid is None:
        return fallback
    match = _GID_TAIL_RE.search(str(gid))
    if not match:
        return fallback
    try:
        return int(match.group(1))
    except (ValueError, TypeError):
        return fallback


def _stable_id(key: str) -> int:
    """Deterministic id derived from a key string alone.

    For tags and aggregated attributes there is no Shopify GID to borrow —
    a tag is a bare string, and a "global attribute" is something we
    synthesise by grouping option names across products. Numbering them by
    enumeration order made their ids a function of catalog *content*, so
    adding one product with a new option value renumbered unrelated
    attributes.

    Hashing the key instead makes the id a function of the key alone: the
    same tag slug gets the same id on every load, forever, regardless of what
    else is in the catalog — and, because it is pure, the per-product copy
    and the aggregated global copy always agree.

    blake2b rather than the builtin ``hash()``: string hashing is salted per
    process, so ``hash()`` would produce different ids after every restart —
    the same bug in a less obvious costume.
    """
    digest = hashlib.blake2b((key or "").encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") or 1


def _warn_on_id_collisions(label: str, entries: List[dict], id_key: str, name_key: str):
    """Log loudly if two distinct keys hashed to the same id.

    A 32-bit space makes this vanishingly unlikely at catalog scale, but the
    consequence (one entry silently overwriting another in loader.tag_by_id /
    attribute_by_id) is invisible otherwise, so it is worth a line in the log.
    """
    seen: Dict[int, str] = {}
    for entry in entries:
        _id = entry.get(id_key)
        _name = str(entry.get(name_key, ""))
        if _id in seen and seen[_id] != _name:
            logger.error(
                f"ShopifyFetcher: {label} id collision — {_name!r} and "
                f"{seen[_id]!r} both hash to {_id}. One will be dropped from "
                f"the id index; rename one of them to resolve."
            )
        else:
            seen[_id] = _name


def _money(val) -> Optional[Decimal]:
    """Parse a Shopify money string to Decimal, or None if absent/unparseable.

    Decimal rather than float: these values are compared for strict
    inequality to decide whether something is on sale, and float rounding
    turns "19.99" vs "19.99" into a discount of 2e-15.
    """
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _sale_split(price, compare_at) -> Tuple[str, str, str]:
    """Map Shopify's (price, compareAtPrice) onto Woo's (price, regular, sale).

    The two models do NOT line up field for field, and the previous mapping
    had them inverted:

      Shopify — ``price`` is what the shopper pays right now. ``compareAtPrice``
        is the ORIGINAL "was" price, and is only meaningful when it is
        strictly GREATER than ``price``. Merchants routinely leave it set to
        the same value as price, or lower, after a promotion ends; Shopify
        renders those as not-on-sale and so must we.

      Woo — ``regular_price`` is the list price, ``sale_price`` is the
        DISCOUNTED price and is EMPTY when nothing is discounted, and
        ``price`` is what the shopper pays now.

    So ``sale_price = compareAtPrice`` was wrong twice over: it put the
    higher original price in the field the widget renders as the sale price,
    and it filled that field on every variant that had a compareAtPrice at
    all. ``formatters.py`` (``is_on_sale = bool(sale_price_raw)``) derives the
    on-sale flag from mere presence, so the widget showed a SALE badge on
    everything, with the "sale" price above the real one.

    Returns ``(price, regular_price, sale_price)`` as strings, matching the
    string-typed money fields the Woo fetcher produces.
    """
    price_str = str(price) if price not in (None, "") else ""
    p = _money(price)
    c = _money(compare_at)

    if p is not None and c is not None and c > p:
        # Genuinely discounted: original goes to regular, current to sale.
        return price_str, str(compare_at), price_str

    # Not on sale — leave sale_price empty so nothing downstream flags it.
    return price_str, price_str, ""


def _normalise_collection(c: dict, idx: int) -> dict:
    """Shopify collection → Woo-shaped category dict consumed by lookup_builder."""
    return {
        # Permanent numeric from the GID, not the position in this fetch —
        # see _gid_numeric. idx is kept only as a degraded fallback.
        "id":          _gid_numeric(c.get("id"), idx + 1),
        "name":        c.get("title", ""),
        "slug":        c.get("handle", ""),
        "description": c.get("description") or "",
        "parent":      0,                  # Shopify collections have no parent
        "count":       int((c.get("productsCount") or {}).get("count", 0)),
        "_shopify_gid": c.get("id"),       # preserve native id in backend_ref
    }


def _normalise_product(p: dict, idx: int, store_domain: str = "") -> dict:
    """Shopify product → Woo-shaped product dict consumed by lookup_builder."""
    variants = [edge["node"] for edge in (p.get("variants") or {}).get("edges", [])]
    images   = [edge["node"] for edge in (p.get("images")   or {}).get("edges", [])]
    cat_edges = (p.get("collections") or {}).get("edges", [])

    # Variants → Woo-shaped variation dicts
    variation_dicts = []
    any_variant_on_sale = False
    for v in variants:
        v_price, v_regular, v_sale = _sale_split(
            v.get("price"), v.get("compareAtPrice")
        )
        if v_sale:
            any_variant_on_sale = True
        variation_dicts.append({
            "id":            v.get("id"),
            "sku":           v.get("sku") or "",
            "price":         v_price,
            "regular_price": v_regular,
            "sale_price":    v_sale,
            "on_sale":       bool(v_sale),
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
        # Permanent numeric from the GID — see _gid_numeric. Previously
        # idx + 1, i.e. the product's POSITION in the fetch result, which
        # silently repointed every id after a deleted product on the next
        # 6-hourly refresh while parked conversation state still held the old
        # numbers. idx is kept only as a degraded fallback.
        #
        # This also fixes fetch_single_product(), which calls this function
        # with idx=0 and therefore used to mint id=1 — colliding with the
        # first product in the catalog.
        "id":            _gid_numeric(p.get("id"), idx + 1),
        "name":          p.get("title", ""),
        "slug":          p.get("handle", ""),
        "type":          "variable" if len(variants) > 1 else "simple",
        "status":        (p.get("status") or "active").lower(),
        "description":   p.get("descriptionHtml") or "",
        "short_description": "",
        "sku":           variants[0].get("sku") if variants else "",
        "price":         min_price,
        "regular_price": min_price,
        # Parent-level sale_price stays empty and on_sale is derived from the
        # variants — the same shape WC REST returns for a variable product,
        # where the parent carries the flag but no single sale price (each
        # variation has its own). formatters.py reads on_sale for the product
        # card and sale_price for the detail panel, so this gives an accurate
        # SALE badge without inventing a parent price that does not exist.
        "sale_price":    "",
        "on_sale":       any_variant_on_sale,
        "stock_status":  "instock" if (p.get("totalInventory") or 0) > 0 else "outofstock",
        "in_stock":      (p.get("totalInventory") or 0) > 0,
        "permalink":     f"https://{store_domain}/products/{p.get('handle','')}",
        "categories": [
            {
                # Same GID-derived id the global categories list uses, so a
                # product's category id and loader.category_by_id agree.
                # Previously this was the index within THIS product's
                # collection edges, which matched nothing.
                "id":   _gid_numeric(e["node"].get("id"), i + 1),
                "name": e["node"].get("title", ""),
                "slug": e["node"].get("handle", ""),
            }
            for i, e in enumerate(cat_edges)
        ],
        "tags": [
            # Shopify tags are bare strings with no GID, so the id is a
            # stable hash of the slug — the same value _aggregate_tags
            # assigns, so per-product and global tag ids always agree.
            {"id": _stable_id(_slugify(t)), "name": t, "slug": _slugify(t)}
            for t in (p.get("tags") or [])
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
                        # Was len(slot) + 1 — the term's position within
                        # whichever product happened to introduce it first,
                        # so the whole term numbering shifted whenever a
                        # product was added or removed. Hash the term slug
                        # instead: same value, same id, every load.
                        "id":    _stable_id(_slugify(value)),
                        "name":  value,
                        "slug":  _slugify(value),
                        "count": 0,
                    }
                slot[value]["count"] += 1

    aggregated = []
    for name, term_map in by_name.items():
        terms = list(term_map.values())
        _warn_on_id_collisions(f"attribute term ({name})", terms, "id", "slug")
        aggregated.append({
            # Was idx + 1 over dict insertion order, i.e. a function of
            # catalog iteration order. attribute_id is cast to int in
            # load_from_shopify's attribute_terms map and used as a dict key
            # in lookup_builder.attribute_by_id — neither needs it dense or
            # small, only stable.
            "attribute_id":    _stable_id(_slugify(name)),
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
                    # Was len(seen) + 1 — discovery order across the whole
                    # catalog, so every tag id moved when a product carrying
                    # an earlier-sorted tag was removed. Same pure hash
                    # _normalise_product uses, so the two agree by
                    # construction rather than by convention.
                    "id":    _stable_id(slug),
                    "name":  tag.get("name", ""),
                    "slug":  slug,
                    "count": 0,
                }
            if slug:
                seen[slug]["count"] += 1
    tags = list(seen.values())
    _warn_on_id_collisions("tag", tags, "id", "slug")
    return tags


# ══════════════════════════════════════════════════════════════
# SINGLE-PRODUCT LIVE FETCH (cache-miss fallback)
# ══════════════════════════════════════════════════════════════

_SINGLE_PRODUCT_QUERY = """
query Product($id: ID!) {
  product(id: $id) {
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
          id title sku price compareAtPrice
          availableForSale inventoryQuantity
          selectedOptions { name value }
          image { url }
        }
      }
    }
  }
}
"""


def fetch_single_product(
    store_domain: str, admin_token: str, product_gid: str
) -> Optional[dict]:
    """
    Fetch one Shopify product by GID and return it in the same Woo-shaped dict
    that _normalise_product() produces for the bulk loader.

    Used as a live fallback in variant_handler when the store_loader cache misses
    a product (e.g. a product added after the last background refresh).

    Returns the normalised product dict on success, or None on any failure.
    The returned dict has the same shape as store_loader.products entries,
    including a top-level ``"variations"`` list ready for variant selection.
    """
    try:
        session = requests.Session()
        data = _gql(
            session, store_domain, admin_token,
            _SINGLE_PRODUCT_QUERY,
            variables={"id": product_gid},
        )
        raw = data.get("product")
        if not raw:
            logger.warning(
                f"ShopifyFetcher.fetch_single_product: "
                f"product(id={product_gid}) returned null — product may not exist"
            )
            return None
        normalised = _normalise_product(raw, idx=0, store_domain=store_domain)
        logger.info(
            f"ShopifyFetcher.fetch_single_product: fetched '{normalised.get('name')}' "
            f"with {len(normalised.get('variations', []))} variations"
        )
        return normalised
    except Exception as e:
        logger.error(
            f"ShopifyFetcher.fetch_single_product({product_gid}) failed: {e}",
            exc_info=True,
        )
        return None


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
    products = [_normalise_product(p, i, store_domain=store_domain) for i, p in enumerate(raw_products)]

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