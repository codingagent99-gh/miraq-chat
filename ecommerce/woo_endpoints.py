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
"""

from typing import List, Optional

from models import WooAPICall


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
