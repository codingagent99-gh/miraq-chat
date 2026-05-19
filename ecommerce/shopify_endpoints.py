"""
ecommerce/shopify_endpoints.py — Shopify implementation of EcommerceEndpoints.

Phase C status
──────────────
✅ All response parsers — fully implemented and used by ShopifyQueryExecutor.
✅ build_cart_variation_payload — in-memory variant resolution via store_loader.
🔲 API-call methods (orders, customers, etc.) — stubbed as
     WooAPICall(surface="shopify_admin"). Phase D wires shopify_client.execute()
     to dispatch these against the Shopify Admin GraphQL API.

products_advanced() raises NotImplementedError by design.
The Shopify executor path does in-memory filtering and must never call it.
Any accidental direct caller will get a clear error immediately.
"""

from typing import Dict, List, Optional

from chat_logger import get_logger
from models import WooAPICall

logger = get_logger("miraq_chat")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _stub(
    method: str,
    endpoint: str,
    *,
    params: dict = None,
    body: dict = None,
    description: str = "",
    requires_resolution=None,
) -> WooAPICall:
    """Return a typed WooAPICall stub for a future shopify_client.execute() call.

    surface="shopify_admin" is the routing key shopify_client will check
    when it is implemented in Phase D.
    """
    return WooAPICall(
        method=method,
        endpoint=endpoint,
        params=params or {},
        body=body,
        surface="shopify_admin",
        description=description,
        requires_resolution=requires_resolution or [],
    )


def _normalize_shopify_address(addr: dict) -> dict:
    """Normalize a Shopify address block to the neutral 6-key shape.

    Handles both GraphQL camelCase (address1, zip, province) and
    REST snake_case (address_1, postcode, state) field names.
    """
    addr = addr or {}
    return {
        "address_1": addr.get("address1") or addr.get("address_1", ""),
        "address_2": addr.get("address2") or addr.get("address_2", ""),
        "city":      addr.get("city", ""),
        "state":     addr.get("province") or addr.get("state", ""),
        "postcode":  addr.get("zip") or addr.get("postcode", ""),
        "country":   addr.get("country", ""),
    }


# ── Endpoint class ────────────────────────────────────────────────────────────

class ShopifyEndpoints:
    """
    Shopify implementation of EcommerceEndpoints.

    Instantiated by ecommerce/__init__.py when ECOMMERCE_BACKEND=shopify.
    ShopifyQueryExecutor depends on parse_product() and
    build_cart_variation_payload(); all other callers get Phase D stubs.
    """

    # ── Store metadata ────────────────────────────────────────────────────────

    def fetch_currency(
        self, description: str = "", requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", "/shop/currency",
                     description=description or "Fetch Shopify store currency",
                     requires_resolution=requires_resolution)

    def list_attributes(
        self, description: str = "", requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        # Shopify has no global attribute taxonomy; shopify_fetcher aggregates
        # options from products at load time. Use store_loader directly.
        return _stub("GET", "/products/attributes",
                     description=description or "List Shopify product option types",
                     requires_resolution=requires_resolution)

    def list_categories(
        self, page: int, per_page: int = 100, description: str = "",
        requires_resolution: Optional[List[str]] = None, **extra_params
    ) -> WooAPICall:
        return _stub("GET", "/collections",
                     params={"first": per_page},
                     description=description or "List Shopify collections",
                     requires_resolution=requires_resolution)

    def list_tags(
        self, page: int, per_page: int = 100, description: str = "",
        requires_resolution: Optional[List[str]] = None, **extra_params
    ) -> WooAPICall:
        return _stub("GET", "/products/tags",
                     description=description or "List Shopify product tags",
                     requires_resolution=requires_resolution)

    def list_published_products(
        self, page: int, per_page: int = 100, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", "/products",
                     params={"first": per_page, "status": "ACTIVE"},
                     description=description or "List Shopify published products",
                     requires_resolution=requires_resolution)

    # ── Products ─────────────────────────────────────────────────────────────

    def fetch_product(
        self, product_id: int, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", f"/products/{product_id}",
                     description=description or f"Fetch Shopify product id={product_id}",
                     requires_resolution=requires_resolution)

    def fetch_variant(
        self, product_id: int, variant_id: int, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", f"/products/{product_id}/variants/{variant_id}",
                     description=description or (
                         f"Fetch Shopify variant product_id={product_id} "
                         f"variant_id={variant_id}"
                     ),
                     requires_resolution=requires_resolution)

    def list_variants(
        self, product_id: int, page: int = 1, per_page: int = 100,
        description: str = "", requires_resolution: Optional[List[str]] = None,
        **extra_params
    ) -> WooAPICall:
        return _stub("GET", f"/products/{product_id}/variants",
                     params={"first": per_page},
                     description=description or f"List Shopify variants product_id={product_id}",
                     requires_resolution=requires_resolution)

    def products_advanced(
        self, body: dict, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        """Intentionally disabled.

        The Shopify path must go through ShopifyQueryExecutor.execute(),
        which filters store_loader.products in-memory.  Raising here catches
        any remaining direct callers that bypassed the executor layer.
        """
        raise NotImplementedError(
            "ShopifyEndpoints.products_advanced() must not be called directly. "
            "Route all product queries through ShopifyQueryExecutor.execute()."
        )

    def build_cart_variation_payload(
        self,
        *,
        product_id: int,
        variant_id: Optional[int],
        resolved_attrs: Dict[str, str],
        store_loader,
    ) -> List[Dict[str, str]]:
        """Build the Shopify cart-line payload for an add-to-cart action.

        Shopify cart lines are keyed by variant GID, not by attribute
        taxonomy/slug pairs as in WooCommerce.

        Priority:
          1. variant_id already known → use it directly.
          2. Walk store_loader.products in-memory to find the variant whose
             selectedOptions match resolved_attrs (case-insensitive).
             Checks both synthetic numeric id and _shopify_gid so callers
             using either form are handled correctly.
        """
        if variant_id:
            return [{"variant_id": str(variant_id)}]

        if not store_loader or not resolved_attrs:
            return []

        for product in (store_loader.products or []):
            p_id      = product.get("id")
            p_gid     = str(product.get("_shopify_gid", ""))
            p_id_str  = str(product_id)

            if p_id != product_id and p_gid != p_id_str:
                continue

            for var in (product.get("variations") or []):
                var_opts = {
                    a.get("name", "").lower(): a.get("option", "").lower()
                    for a in (var.get("attributes") or [])
                    if isinstance(a, dict)
                }
                if all(
                    var_opts.get(k.lower()) == v.lower()
                    for k, v in resolved_attrs.items()
                ):
                    vid = var.get("_shopify_gid") or var.get("id")
                    if vid:
                        return [{"variant_id": str(vid)}]

        logger.warning(
            "ShopifyEndpoints.build_cart_variation_payload: no variant match "
            f"for product_id={product_id} attrs={resolved_attrs}"
        )
        return []

    # ── Orders ────────────────────────────────────────────────────────────────

    def list_customer_orders(
        self, customer_id, page: int, per_page: int = 5, description: str = "",
        requires_resolution: Optional[List[str]] = None, **filters
    ) -> WooAPICall:
        return _stub("GET", f"/customers/{customer_id}/orders",
                     params={"first": per_page, **filters},
                     description=description or f"List Shopify orders customer={customer_id}",
                     requires_resolution=requires_resolution)

    def fetch_order(
        self, order_id: int, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", f"/orders/{order_id}",
                     description=description or f"Fetch Shopify order id={order_id}",
                     requires_resolution=requires_resolution)

    def check_stock(
        self, product_ids: List[int], description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        # Shopify stock is embedded in the in-memory product/variant objects.
        # For real-time checks use ShopifyQueryExecutor with product_id kwarg.
        return _stub("GET", "/products/stock-check",
                     params={"ids": product_ids},
                     description=description or "Check Shopify stock status",
                     requires_resolution=requires_resolution)

    def create_order(
        self, payload: dict, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("POST", "/orders",
                     body=payload,
                     description=description or "Create Shopify order",
                     requires_resolution=requires_resolution)

    def historical_product_search(
        self, body: dict, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("POST", "/products/historical-search",
                     body=body,
                     description=description or "Shopify historical product search",
                     requires_resolution=requires_resolution)

    # ── Customers ─────────────────────────────────────────────────────────────

    def fetch_customer(
        self, customer_id: int, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", f"/customers/{customer_id}",
                     description=description or f"Fetch Shopify customer id={customer_id}",
                     requires_resolution=requires_resolution)

    def update_customer(
        self, customer_id: int, payload: dict, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("PUT", f"/customers/{customer_id}",
                     body=payload,
                     description=description or f"Update Shopify customer id={customer_id}",
                     requires_resolution=requires_resolution)

    # ── Additional ────────────────────────────────────────────────────────────

    def list_coupons(
        self, page: int, per_page: int, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", "/discount_codes",
                     params={"first": per_page},
                     description=description or "List Shopify discount codes",
                     requires_resolution=requires_resolution)

    def fetch_wishlist(
        self, customer_id, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        # Shopify has no native wishlist — typically a customer metafield or app.
        return _stub("GET", f"/customers/{customer_id}/wishlist",
                     description=description or "Fetch Shopify wishlist (metafield)",
                     requires_resolution=requires_resolution)

    def list_products_on_sale(
        self, page: int, per_page: int, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", "/products/on-sale",
                     params={"first": per_page},
                     description=description or "List Shopify products on sale",
                     requires_resolution=requires_resolution)

    def list_attribute_terms(
        self, attribute_id: int, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", f"/products/attributes/{attribute_id}/terms",
                     description=description or f"List Shopify attribute terms id={attribute_id}",
                     requires_resolution=requires_resolution)

    def search_products(
        self, search_term: str, page: int, per_page: int, status: str = "publish",
        description: str = "", requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("GET", "/products/search",
                     params={"query": search_term, "first": per_page},
                     description=description or f"Shopify text search for '{search_term}'",
                     requires_resolution=requires_resolution)

    def list_cs_orders(
        self, body: dict, description: str = "",
        requires_resolution: Optional[List[str]] = None
    ) -> WooAPICall:
        return _stub("POST", "/orders/cs-list",
                     body=body,
                     description=description or "CS rep Shopify order list",
                     requires_resolution=requires_resolution)

    # ── Response parsers ──────────────────────────────────────────────────────
    # All parsers handle two shapes:
    #   • shopify_fetcher-normalised dicts (already Woo-ish, in_stock bool present)
    #   • Raw Shopify Admin API responses (camelCase, GID ids, province/zip)
    # Phase D callers can pass raw API responses directly once shopify_client
    # is wired up.

    def parse_product(self, response: dict) -> dict:
        """Normalise a Shopify product dict into a backend-neutral dict.

        Returns:
            {"id", "price": str, "in_stock": bool, "_raw": dict}
        """
        price = (
            response.get("sale_price")
            or response.get("price")
            or response.get("regular_price")
            or ""
        )
        in_stock = response.get("in_stock")
        if in_stock is None:
            in_stock = (
                response.get("stock_status") == "instock"
                or (response.get("totalInventory") or 0) > 0
            )
        return {
            "id":       response.get("id"),
            "price":    str(price) if price else "",
            "in_stock": bool(in_stock),
            "_raw":     response,
        }

    def parse_variant(self, response: dict) -> dict:
        """Normalise a Shopify variant dict into a backend-neutral dict.

        Handles shopify_fetcher's attributes=[{name, option}] shape and the
        raw Admin API's selectedOptions=[{name, value}] shape.

        Returns:
            {"id", "price": str, "options": dict[str,str], "in_stock": bool, "_raw": dict}
        """
        price = response.get("price") or response.get("regular_price") or ""
        options: Dict[str, str] = {}

        attrs = response.get("attributes") or []
        if isinstance(attrs, list):
            for a in attrs:
                if not isinstance(a, dict):
                    continue
                name = a.get("name", "")
                # shopify_fetcher uses "option"; raw Admin API uses "value"
                val = a.get("option") or a.get("value", "")
                if name and val:
                    options[name] = val
        elif isinstance(attrs, dict):
            options = {k: v for k, v in attrs.items() if v}

        in_stock = response.get("in_stock")
        if in_stock is None:
            in_stock = (
                response.get("stock_status") == "instock"
                or bool(response.get("availableForSale"))
            )

        return {
            "id":       response.get("id"),
            "price":    str(price) if price else "",
            "options":  options,
            "in_stock": bool(in_stock),
            "_raw":     response,
        }

    def parse_list_variants(self, response: list) -> List[dict]:
        if not isinstance(response, list):
            return []
        return [self.parse_variant(v) for v in response if isinstance(v, dict)]

    def parse_order(self, response: dict) -> dict:
        """Normalise a Shopify order into a backend-neutral dict.

        Derives a Woo-compatible ``status`` string from Shopify's separate
        financialStatus + fulfillmentStatus fields (GraphQL camelCase) or
        financial_status + fulfillment_status (REST snake_case).

        Mapping:
            fulfilled / shipped  → "completed"
            paid / partially_paid (not fulfilled) → "processing"
            pending              → "pending"
            refunded / partially_refunded → "refunded"
            voided               → "cancelled"
            <other>              → raw financial or fulfillment string

        Returns:
            {"id", "status": str, "billing_address": dict,
             "shipping_address": dict, "_raw": dict}
        """
        financial = (
            response.get("financialStatus") or response.get("financial_status", "")
        ).lower()
        fulfillment = (
            response.get("fulfillmentStatus") or response.get("fulfillment_status", "")
        ).lower()

        if fulfillment in ("fulfilled", "shipped"):
            status = "completed"
        elif financial in ("paid", "partially_paid"):
            status = "processing"
        elif financial == "pending":
            status = "pending"
        elif financial in ("refunded", "partially_refunded"):
            status = "refunded"
        elif financial == "voided":
            status = "cancelled"
        else:
            status = financial or fulfillment or "unknown"

        return {
            "id":               response.get("id"),
            "status":           status,
            "billing_address":  _normalize_shopify_address(
                response.get("billingAddress") or response.get("billing_address")
            ),
            "shipping_address": _normalize_shopify_address(
                response.get("shippingAddress") or response.get("shipping_address")
            ),
            "_raw":             response,
        }

    def parse_customer(self, response: dict) -> dict:
        """Normalise a Shopify customer into a backend-neutral dict.

        Handles GraphQL camelCase keys and connection edge unwrapping for the
        ``addresses`` field, as well as REST snake_case keys.

        Returns:
            {"id", "first_name", "last_name", "email",
             "default_address": dict, "addresses": list[dict], "_raw": dict}
        """
        default_raw = (
            response.get("defaultAddress") or response.get("default_address") or {}
        )
        default_addr = _normalize_shopify_address(default_raw)

        raw_addrs = response.get("addresses") or []
        # Unwrap GraphQL connection edges when present
        if raw_addrs and isinstance(raw_addrs[0], dict) and "node" in raw_addrs[0]:
            raw_addrs = [e["node"] for e in raw_addrs]

        addresses = (
            [_normalize_shopify_address(a) for a in raw_addrs]
            if raw_addrs
            else ([default_addr] if any(default_addr.values()) else [])
        )

        return {
            "id":              response.get("id"),
            "first_name":      response.get("firstName") or response.get("first_name", ""),
            "last_name":       response.get("lastName") or response.get("last_name", ""),
            "email":           response.get("email", ""),
            "default_address": default_addr,
            "addresses":       addresses,
            "_raw":            response,
        }

    def parse_list_published_products(self, response: list) -> List[dict]:
        if not isinstance(response, list):
            return []
        return [self.parse_product(p) for p in response if isinstance(p, dict)]
