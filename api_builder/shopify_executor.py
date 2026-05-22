"""
api_builder/shopify_executor.py — In-memory query executor for Shopify.

Evaluates the serialized query body produced by serialize_query() directly
against store_loader.products — no HTTP calls, no GraphQL round-trips at
query time.

Implements the same QueryExecutor protocol as WooQueryExecutor so all
callers that depend on the protocol are unaffected.

Taxonomy resolution
───────────────────
filter_builder emits taxonomy strings in Woo-shaped form:
  - "product_cat"      → matched against product.categories[].slug
  - "product_tag"      → matched against product.tags[].slug
  - "pa_color"         → stripped to "color", matched against product.attributes
  - "color"            → matched against product.attributes directly

lookup_builder strips "pa_" when building attribute_by_key, so we mirror
that stripping here to keep the two indexes in sync.
"""

from typing import Optional

from chat_logger import get_logger
from api_builder.query_tree import serialize_query

logger = get_logger("miraq_chat")


# ══════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ══════════════════════════════════════════════════════════════

def _slugify(s: str) -> str:
    """Minimal slug normalisation — mirrors shopify_fetcher._slugify."""
    return (
        (s or "")
        .strip()
        .lower()
        .replace("'", "")
        .replace('"', "")
        .replace("/", "-")
        .replace(" ", "-")
    )


def _parse_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _product_price(product: dict) -> float:
    """Return the cheapest applicable price for a product as a float."""
    return _parse_float(
        product.get("sale_price")
        or product.get("price")
        or product.get("regular_price")
        or 0
    )


def _normalize_product(product: dict) -> dict:
    """
    Convert a shopify_fetcher-normalised product dict into the backend-neutral
    shape that WooQueryExecutor also returns:
        {"id", "price": str, "in_stock": bool, "_raw": dict}
    """
    price = (
        product.get("sale_price")
        or product.get("price")
        or product.get("regular_price")
        or ""
    )
    in_stock = product.get("in_stock")
    if in_stock is None:
        in_stock = product.get("stock_status") == "instock"
    return {
        "id":       product.get("id"),
        "price":    str(price) if price else "",
        "in_stock": bool(in_stock),
        "_raw":     product,
    }


# ══════════════════════════════════════════════════════════════
# CONDITION EVALUATION
# ══════════════════════════════════════════════════════════════

def _match_attribute(product: dict, attr_key: str, terms: set) -> bool:
    """
    True when the product has an attribute whose slugified name equals
    attr_key AND at least one option value slug is in terms.

    shopify_fetcher stores product-level attributes as:
        [{"name": "Color", "options": ["Red", "Blue"]}, ...]
    Terms arrive as slugs (e.g. {"red", "blue"}), so we slugify each value
    before comparing.
    """
    for attr in (product.get("attributes") or []):
        if _slugify(attr.get("name", "")) != attr_key:
            continue
        for value in (attr.get("options") or []):
            if _slugify(str(value)) in terms:
                return True
    return False


def _taxonomy_match(product: dict, taxonomy: str, terms: set, loader) -> bool:
    """
    Resolve which catalog dimension the taxonomy string names, then test the
    product against that dimension.

    Resolution order
    ────────────────
    1. Known attribute key (after pa_ strip) → product.attributes
    2. "product_cat" or slug in category_by_key → product.categories
    3. "product_tag" or slug in tag_by_key → product.tags
    4. Unknown → try all three (graceful fallback)
    """
    # Woo emits attribute taxonomies as "pa_color"; strip the prefix so it
    # matches the attribute_by_key index built by lookup_builder.
    attr_key = taxonomy.removeprefix("pa_")

    if attr_key in loader.attribute_by_key:
        return _match_attribute(product, attr_key, terms)

    if taxonomy == "product_cat" or attr_key in loader.category_by_key:
        cat_slugs = {c.get("slug", "") for c in (product.get("categories") or [])}
        return bool(cat_slugs & terms)

    if taxonomy == "product_tag" or attr_key in loader.tag_by_key:
        tag_slugs = {t.get("slug", "") for t in (product.get("tags") or [])}
        return bool(tag_slugs & terms)

    # Graceful fallback: try every dimension so unknown taxonomies don't
    # silently exclude products that would otherwise match.
    cat_slugs = {c.get("slug", "") for c in (product.get("categories") or [])}
    tag_slugs = {t.get("slug", "") for t in (product.get("tags") or [])}
    return (
        bool((cat_slugs | tag_slugs) & terms)
        or _match_attribute(product, attr_key, terms)
    )


def _eval_condition(product: dict, condition: dict, loader) -> bool:
    """
    Recursively evaluate one query-tree node against a product.

    Handles:
      - OR/AND group nodes  {"relation": "OR"|"AND", "conditions": [...]}
      - Taxonomy leaf nodes {"taxonomy": ..., "terms": [...], "operator": "IN"|"NOT IN"}
      - field_type leaves   (price/stock/search — handled before this call in
                            _apply_body; guard here in case a malformed body
                            embeds them inside the filters block)
    """
    if "relation" in condition:
        subs = condition.get("conditions") or []
        if condition.get("relation", "AND").upper() == "OR":
            return any(_eval_condition(product, sub, loader) for sub in subs)
        return all(_eval_condition(product, sub, loader) for sub in subs)

    if condition.get("field_type"):
        # Should not appear inside filters; pass-through to avoid false excludes.
        return True

    taxonomy = condition.get("taxonomy", "")
    terms    = set(condition.get("terms") or [])
    operator = condition.get("operator", "IN").upper()

    if not terms:
        return True

    matched = _taxonomy_match(product, taxonomy, terms, loader)
    return matched if operator == "IN" else not matched


def _passes_filters(product: dict, filters: dict, loader) -> bool:
    """Apply the top-level filters block (AND/OR of conditions) to a product."""
    if not filters:
        return True
    rel   = filters.get("relation", "AND").upper()
    conds = filters.get("conditions") or []
    if rel == "OR":
        return any(_eval_condition(product, c, loader) for c in conds)
    return all(_eval_condition(product, c, loader) for c in conds)


# ══════════════════════════════════════════════════════════════
# EXECUTOR
# ══════════════════════════════════════════════════════════════

class ShopifyQueryExecutor:
    """
    In-memory query executor for Shopify.

    Accepts the same (conditions, page, per_page) interface as
    WooQueryExecutor.execute() and returns the identical response shape::

        {
            "products": [{"id", "price", "in_stock", "_raw"}, ...],
            "page":     int,
            "per_page": int,
            "total":    int,
            "pages":    int,
            "_raw":     dict,
        }

    Usage::

        executor = ShopifyQueryExecutor(store_loader)
        result   = executor.execute(conditions, page=1, per_page=20)
    """

    def __init__(self, store_loader):
        """
        Args:
            store_loader: A fully loaded StoreLoader instance
                (ECOMMERCE_BACKEND=shopify).  Requires .products,
                .attribute_by_key, .category_by_key, .tag_by_key.
        """
        self._loader = store_loader

    def execute(
        self,
        conditions: list,
        page: int,
        per_page: int,
        *,
        product_id: Optional[int] = None,
        search_term: Optional[str] = None,
    ) -> dict:
        """
        Execute a query tree against the in-memory catalog.

        Args:
            conditions: List of query-tree nodes (from filter_builder or
                        query_tree.py node constructors).
            page:       1-based page number.
            per_page:   Number of results per page.
            product_id: When set, return only the product with this synthetic
                        id (bypasses all other filters).
            search_term: Ignored here — callers should encode free-text intent
                         as a make_search_condition node in conditions instead.
                         Kept for protocol parity with WooQueryExecutor.
        """
        body = serialize_query(conditions, page, per_page)

        if product_id is not None:
            matched = [
                p for p in (self._loader.products or [])
                if (
                    p.get("id") == product_id
                    or str(p.get("_shopify_gid", "")) == str(product_id)
                )
            ]
        else:
            matched = self._apply_body(body)

        total  = len(matched)
        start  = (page - 1) * per_page
        sliced = matched[start : start + per_page]
        pages  = max(1, -(-total // per_page)) if total else 0

        return {
            "products": [_normalize_product(p) for p in sliced],
            "page":     page,
            "per_page": per_page,
            "total":    total,
            "pages":    pages,
            "_raw":     {"products": sliced},
        }

    def execute_from_body(self, body: dict) -> dict:
        logger.debug(f"[ShopifyExecutor] loader.products count = {len(self._loader.products or [])}")
        page     = body.get("page", 1)
        per_page = body.get("per_page", 4)
        ids      = body.get("ids")

        if ids:
            id_set = {str(i) for i in ids}
            matched = [
                p for p in (self._loader.products or [])
                if (
                    str(p.get("id", "")) in id_set
                    or str(p.get("_shopify_gid", "")) in id_set
                )
            ]
        else:
            matched = self._apply_body(body)

        total  = len(matched)
        start  = (page - 1) * per_page
        sliced = matched[start : start + per_page]
        pages  = max(1, -(-total // per_page)) if total else 0
        
        # Right before the return in execute_from_body
        for p in sliced:
            if not p.get("_shopify_gid"):
                continue  # WooCommerce products — untouched
            variations = p.get("variations", [])
            if variations:
                any_in_stock = any(
                    v.get("in_stock") or v.get("stock_status") == "instock"
                    for v in variations
                )
            else:
                # No variations — fall back to whatever is already set
                any_in_stock = p.get("in_stock") or p.get("stock_status") == "instock"
            p["in_stock"] = any_in_stock
            p["stock_status"] = "instock" if any_in_stock else "outofstock"
                
        return {
            "products": sliced,          # ← raw dicts, not _normalize_product()
            "page":     page,
            "per_page": per_page,
            "total":    total,
            "pages":    pages,
            "_raw":     {"products": sliced},
        }
    # ── private ──────────────────────────────────────────────

    def _apply_body(self, body: dict) -> list:
        """
        Filter store_loader.products against every clause in the serialized
        query body.  Returns the list of matching product dicts (unsliced).

        Evaluation order mirrors the Woo plugin's own precedence:
          1. stock_status  — fast string compare, cheapest check first
          2. price range   — numeric compare
          3. free-text     — substring scan over name + descriptions
          4. taxonomy      — recursive condition tree eval
        """
        loader   = self._loader
        stock_f  = body.get("stock_status")
        price_f  = body.get("price")
        search_f = (body.get("search") or "").lower().strip()
        filters  = body.get("filters")

        results = []
        for product in (loader.products or []):

            # ── 1. stock status ───────────────────────────────────────────
            if stock_f:
                product_stock = (
                    "instock" if product.get("in_stock")
                    else product.get("stock_status", "outofstock")
                )
                if product_stock != stock_f:
                    continue

            # ── 2. price range ────────────────────────────────────────────
            if price_f:
                price = _product_price(product)
                mn = price_f.get("min")
                mx = price_f.get("max")
                if mn is not None and price < _parse_float(mn):
                    continue
                if mx is not None and price > _parse_float(mx):
                    continue

            # ── 3. free-text search (name + descriptions) ─────────────────
            if search_f:
                haystack = " ".join(filter(None, [
                    (product.get("name") or "").lower(),
                    (product.get("description") or "").lower(),
                    (product.get("short_description") or "").lower(),
                ]))
                if search_f not in haystack:
                    continue

            # ── 4. taxonomy / attribute / tag / category filters ──────────
            if filters and not _passes_filters(product, filters, loader):
                continue

            results.append(product)

        return results