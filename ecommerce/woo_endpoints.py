"""
ecommerce/woo_endpoints.py — WooCommerce concrete implementation of EcommerceEndpoints.

Every WooCommerce URL string lives in this file and nowhere else.
Callers construct API calls via the methods below; ``woo_client.execute()``
handles auth and transport.

Relative paths are used throughout (e.g. ``/orders`` rather than the full URL).
``woo_client.execute()`` prepends the appropriate base URL based on the
``surface`` field:
  - ``"admin"``        → ``WOO_BASE_URL``       (e.g. {WP_BASE}/wp-json/wc/v3)
  - ``"custom_plugin"``→ ``CUSTOM_API_BASE_URL`` (e.g. {WP_BASE}/wp-json/custom-api/v1)

Each ``fetch_*`` / ``list_*`` call constructor is paired with a ``parse_*`` method
that normalizes the raw WooCommerce response dict into a backend-neutral shape.
All parsers include a ``_raw`` key with the original response so callers can
access any Woo-specific field that has not yet been normalized.
"""

from typing import Dict, List, Optional

from models import WooAPICall


# ── Address normalization helper ────────────────────────────────────────────

def _normalize_woo_address(addr: dict) -> dict:
    """Normalize a WooCommerce address sub-dict to the backend-neutral shape.

    WooCommerce address dicts already use the neutral key names
    (``address_1``, ``postcode``, etc.).  A future Shopify parser will remap
    ``address1`` → ``address_1``, ``zip`` → ``postcode``, etc. before calling
    a similar helper so callers always receive the same shape.

    Returns:
        {
            "address_1": str,
            "address_2": str,
            "city": str,
            "state": str,
            "postcode": str,
            "country": str,
        }
    """
    return {
        "address_1": addr.get("address_1", ""),
        "address_2": addr.get("address_2", ""),
        "city": addr.get("city", ""),
        "state": addr.get("state", ""),
        "postcode": addr.get("postcode", ""),
        "country": addr.get("country", ""),
    }


class WooEndpoints:
    """WooCommerce implementation of EcommerceEndpoints."""

    # ── Store metadata ──────────────────────────────────────────────────────

    def fetch_currency(
        self,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.1 — GET /data/currencies/current"""
        return WooAPICall(
            method="GET",
            endpoint="/data/currencies/current",
            params={},
            surface="admin",
            description=description or "Fetch active currency",
            requires_resolution=requires_resolution or [],
        )

    def list_attributes(
        self,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.2 — GET /all-attributes (custom_plugin surface)"""
        return WooAPICall(
            method="GET",
            endpoint="/all-attributes",
            params={},
            surface="custom_plugin",
            description=description or "Fetch all product attributes and terms",
            requires_resolution=requires_resolution or [],
        )

    def list_categories(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 2.3 — GET /products/categories"""
        params = {
            "per_page": per_page,
            "page": page,
            "hide_empty": True,
            **extra_params,
        }
        return WooAPICall(
            method="GET",
            endpoint="/products/categories",
            params=params,
            surface="admin",
            description=description or "List product categories",
            requires_resolution=requires_resolution or [],
        )

    def list_tags(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 2.4 — GET /products/tags"""
        params = {
            "per_page": per_page,
            "page": page,
            "hide_empty": True,
            **extra_params,
        }
        return WooAPICall(
            method="GET",
            endpoint="/products/tags",
            params=params,
            surface="admin",
            description=description or "List product tags",
            requires_resolution=requires_resolution or [],
        )

    def list_published_products(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.5 — GET /products?status=publish"""
        return WooAPICall(
            method="GET",
            endpoint="/products",
            params={"status": "publish", "per_page": per_page, "page": page},
            surface="admin",
            description=description or "List all published products",
            requires_resolution=requires_resolution or [],
        )

    # ── Products ────────────────────────────────────────────────────────────

    def fetch_product(
        self,
        product_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.1 — GET /products/{product_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/products/{product_id}",
            params={},
            surface="admin",
            description=description or f"Fetch product id={product_id}",
            requires_resolution=requires_resolution or [],
        )

    def fetch_variant(
        self,
        product_id: int,
        variant_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.2 — GET /products/{product_id}/variations/{variant_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/products/{product_id}/variations/{variant_id}",
            params={},
            surface="admin",
            description=description or f"Fetch variant product_id={product_id} variant_id={variant_id}",
            requires_resolution=requires_resolution or [],
        )

    def list_variants(
        self,
        product_id: int,
        page: int = 1,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 4.3 — GET /products/{product_id}/variations"""
        params = {"per_page": per_page, "page": page, **extra_params}
        return WooAPICall(
            method="GET",
            endpoint=f"/products/{product_id}/variations",
            params=params,
            surface="admin",
            description=description or f"List variants for product_id={product_id}",
            requires_resolution=requires_resolution or [],
        )

    def products_advanced(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.4 — POST /products-advanced-new (custom_plugin surface)"""
        return WooAPICall(
            method="POST",
            endpoint="/products-advanced-new",
            params={},
            body=body,
            surface="custom_plugin",
            description=description or "Advanced product filter",
            requires_resolution=requires_resolution or [],
        )

    # ── Orders ──────────────────────────────────────────────────────────────

    def list_customer_orders(
        self,
        customer_id,
        page: int,
        per_page: int = 5,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **filters,
    ) -> WooAPICall:
        """Row 5.1 — GET /orders"""
        params = {
            "customer": customer_id,
            "per_page": per_page,
            "page": page,
            "orderby": "date",
            "order": "desc",
            **filters,
        }
        return WooAPICall(
            method="GET",
            endpoint="/orders",
            params=params,
            surface="admin",
            description=description or f"List orders for customer {customer_id}",
            requires_resolution=requires_resolution or [],
        )

    def fetch_order(
        self,
        order_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.2 — GET /orders/{order_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/orders/{order_id}",
            params={},
            surface="admin",
            description=description or f"Fetch order id={order_id}",
            requires_resolution=requires_resolution or [],
        )

    def check_stock(
        self,
        product_ids: List[int],
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.3 — Thin wrapper over products_advanced for stock checking."""
        return self.products_advanced(
            body={"ids": product_ids, "per_page": len(product_ids)},
            description=description or "Check stock status for product IDs",
            requires_resolution=requires_resolution or [],
        )

    def create_order(
        self,
        payload: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.4 — POST /orders"""
        return WooAPICall(
            method="POST",
            endpoint="/orders",
            params={},
            body=payload,
            surface="admin",
            description=description or "Create order",
            requires_resolution=requires_resolution or [],
        )

    def historical_product_search(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.5 — Thin wrapper over products_advanced for historical searches."""
        return self.products_advanced(
            body=body,
            description=description or "Historical product search",
            requires_resolution=requires_resolution or [],
        )

    # ── Customers ───────────────────────────────────────────────────────────

    def fetch_customer(
        self,
        customer_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Rows 6.1 & 6.2 — GET /customers/{customer_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/customers/{customer_id}",
            params={},
            surface="admin",
            description=description or f"Fetch customer id={customer_id}",
            requires_resolution=requires_resolution or [],
        )

    def update_customer(
        self,
        customer_id: int,
        payload: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 6.3 — PUT /customers/{customer_id}"""
        return WooAPICall(
            method="PUT",
            endpoint=f"/customers/{customer_id}",
            params={},
            body=payload,
            surface="admin",
            description=description or f"Update customer id={customer_id}",
            requires_resolution=requires_resolution or [],
        )

    # ── Additional (not in CSV mapping; present in codebase) ────────────────

    def list_coupons(
        self,
        page: int,
        per_page: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List available coupon codes — GET /coupons"""
        return WooAPICall(
            method="GET",
            endpoint="/coupons",
            params={"per_page": per_page, "page": page},
            surface="admin",
            description=description or "List available coupon codes",
            requires_resolution=requires_resolution or [],
        )

    def fetch_wishlist(
        self,
        customer_id,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Fetch customer wishlist — POST /wishlist"""
        return WooAPICall(
            method="POST",
            endpoint="/wishlist",
            params={"customer_id": customer_id},
            surface="admin",
            description=description or "Get customer wishlist",
            requires_resolution=requires_resolution or [],
        )

    def list_products_on_sale(
        self,
        page: int,
        per_page: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List products on sale — GET /products?on_sale=true"""
        return WooAPICall(
            method="GET",
            endpoint="/products",
            params={"on_sale": "true", "per_page": per_page, "page": page, "status": "publish"},
            surface="admin",
            description=description or "List products on sale",
            requires_resolution=requires_resolution or [],
        )

    def list_attribute_terms(
        self,
        attribute_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List terms for a product attribute — GET /products/attributes/{id}/terms"""
        return WooAPICall(
            method="GET",
            endpoint=f"/products/attributes/{attribute_id}/terms",
            params={"per_page": 100},
            surface="admin",
            description=description or f"List terms for attribute id={attribute_id}",
            requires_resolution=requires_resolution or [],
        )

    def search_products(
        self,
        search_term: str,
        page: int,
        per_page: int,
        status: str = "publish",
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Text search for products — GET /products?search=..."""
        return WooAPICall(
            method="GET",
            endpoint="/products",
            params={"search": search_term, "per_page": per_page, "page": page, "status": status},
            surface="admin",
            description=description or f"Text search for '{search_term}'",
            requires_resolution=requires_resolution or [],
        )

    def list_cs_orders(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List orders via custom plugin (CS-rep role) — POST /orders (custom_plugin surface)"""
        return WooAPICall(
            method="POST",
            endpoint="/orders",
            params={},
            body=body,
            surface="custom_plugin",
            description=description or "CS rep order list",
            requires_resolution=requires_resolution or [],
        )

    # ── Response parsers ────────────────────────────────────────────────────
    # Each parser takes the raw ``woo_client.execute(...).get("data")`` dict
    # (or list) and returns a backend-neutral dict (or list of dicts).
    # Every result includes ``_raw`` with the original response so callers can
    # access any not-yet-normalized WooCommerce-specific field during migration.

    def parse_product(self, response: dict) -> dict:
        """Normalize a WooCommerce product response into a backend-neutral dict.

        Args:
            response: Raw product dict from ``woo_client.execute(...).get("data") or {}``.

        Returns:
            {
                "id": int | None,
                "price": str,       # sale_price if set, else price, else regular_price
                "in_stock": bool,   # True when stock_status == "instock"
                "_raw": dict,       # original response for migration safety
            }
        """
        price = (
            response.get("sale_price")
            or response.get("price")
            or response.get("regular_price")
            or ""
        )
        return {
            "id": response.get("id"),
            "price": price,
            "in_stock": response.get("stock_status") == "instock",
            "_raw": response,
        }

    def parse_variant(self, response: dict) -> dict:
        """Normalize a WooCommerce variation response into a backend-neutral dict.

        Args:
            response: Raw variation dict from ``woo_client.execute(...).get("data") or {}``,
                      or an individual item from a variations list.

        Returns:
            {
                "id": int | None,
                "price": str,           # sale_price if set, else price, else regular_price
                "options": dict,        # {attribute_name: option_value}
                "in_stock": bool,       # True when stock_status == "instock"
                "_raw": dict,           # original response for migration safety
            }
        """
        price = (
            response.get("sale_price")
            or response.get("price")
            or response.get("regular_price")
            or ""
        )

        # Build options dict — WooCommerce variations carry a list of attribute dicts
        # [{"name": "Color", "option": "Red"}, ...] or a flat {name: value} dict
        # (custom-plugin format).
        options: Dict[str, str] = {}
        attrs = response.get("attributes", [])
        if isinstance(attrs, list):
            for attr in attrs:
                if isinstance(attr, dict) and attr.get("name") and attr.get("option"):
                    options[attr["name"]] = attr["option"]
        elif isinstance(attrs, dict):
            options = {k: v for k, v in attrs.items() if v}

        return {
            "id": response.get("id"),
            "price": price,
            "options": options,
            "in_stock": response.get("stock_status") == "instock",
            "_raw": response,
        }

    def parse_list_variants(self, response: list) -> List[dict]:
        """Normalize a WooCommerce variations list into backend-neutral dicts.

        Args:
            response: Raw list from ``woo_client.execute(...).get("data") or []``.

        Returns:
            List of dicts, each in the same shape as ``parse_variant``.
        """
        if not isinstance(response, list):
            return []
        return [self.parse_variant(item) for item in response if isinstance(item, dict)]

    def parse_order(self, response: dict) -> dict:
        """Normalize a WooCommerce order response into a backend-neutral dict.

        WooCommerce uses a single ``status`` string.  A future Shopify parser
        will derive an equivalent string from ``financial_status`` +
        ``fulfillment_status`` so callers receive the same shape.

        Address sub-dicts (``billing`` / ``shipping``) are normalized to the
        six-key neutral shape via ``_normalize_woo_address``.  Callers that
        need WooCommerce-specific address fields (e.g. ``billing.first_name``,
        ``billing.email``) should read them from ``_raw["billing"]``.

        Args:
            response: Raw order dict from ``woo_client.execute(...).get("data") or {}``.

        Returns:
            {
                "id": int | None,
                "status": str,
                "billing_address": dict,   # neutral 6-key address shape
                "shipping_address": dict,  # neutral 6-key address shape
                "_raw": dict,              # original response for migration safety
            }
        """
        return {
            "id": response.get("id"),
            "status": response.get("status", ""),
            "billing_address": _normalize_woo_address(response.get("billing", {})),
            "shipping_address": _normalize_woo_address(response.get("shipping", {})),
            "_raw": response,
        }

    def parse_customer(self, response: dict) -> dict:
        """Normalize a WooCommerce customer response into a backend-neutral dict.

        WooCommerce stores address information in two separate ``billing`` and
        ``shipping`` blocks.  The neutral shape uses ``default_address`` (the
        billing address) and ``addresses`` (a list containing both addresses,
        deduplicated when they are identical).

        A future Shopify parser will map ``default_address`` and ``addresses[]``
        from Shopify's native format into the same shape.

        Callers that need WooCommerce-specific address fields (e.g.
        ``billing.first_name``, ``billing.phone``) should read them from
        ``_raw["billing"]``.

        Args:
            response: Raw customer dict from ``woo_client.execute(...).get("data") or {}``.

        Returns:
            {
                "id": int | None,
                "first_name": str,
                "last_name": str,
                "email": str,
                "default_address": dict,   # neutral 6-key shape (billing)
                "addresses": list[dict],   # [billing] or [billing, shipping] if different
                "_raw": dict,              # original response for migration safety
            }
        """
        billing = _normalize_woo_address(response.get("billing", {}))
        shipping = _normalize_woo_address(response.get("shipping", {}))

        # Include shipping only when it differs from billing and is non-empty
        addresses = [billing]
        if shipping != billing and any(shipping.values()):
            addresses.append(shipping)

        return {
            "id": response.get("id"),
            "first_name": response.get("first_name", ""),
            "last_name": response.get("last_name", ""),
            "email": response.get("email", ""),
            "default_address": billing,
            "addresses": addresses,
            "_raw": response,
        }

    def parse_list_published_products(self, response: list) -> List[dict]:
        """Normalize a WooCommerce published-products list into backend-neutral dicts.

        WooCommerce expresses stock availability as ``stock_status == "instock"``.
        A future Shopify parser will derive the same ``in_stock: bool`` from
        ``inventory_quantity > 0`` so callers receive an identical shape.

        Args:
            response: Raw list from ``woo_client.execute(...).get("data") or []``.

        Returns:
            List of dicts:
            {
                "id": int | None,
                "price": str,       # sale_price if set, else price, else regular_price
                "in_stock": bool,   # True when stock_status == "instock"
                "_raw": dict,       # original item for migration safety
            }
        """
        if not isinstance(response, list):
            return []
        return [
            {
                "id": item.get("id"),
                "price": (
                    item.get("sale_price")
                    or item.get("price")
                    or item.get("regular_price")
                    or ""
                ),
                "in_stock": item.get("stock_status") == "instock",
                "_raw": item,
            }
            for item in response
            if isinstance(item, dict)
        ]
