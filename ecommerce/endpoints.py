"""
ecommerce/endpoints.py — Protocol declaring the interface every e-commerce
backend module must implement.

Each function returns a ``WooAPICall`` ready to be passed to
``woo_client.execute()``.  The protocol covers only *call construction*; response
shape normalisation is deferred to Phase 3.

A future ``shopify_endpoints.py`` is a drop-in if it provides a class that
satisfies this Protocol.
"""

from typing import List, Optional, Protocol

from models import WooAPICall


class EcommerceEndpoints(Protocol):
    """Interface for an e-commerce endpoint factory."""

    # ── Store metadata ──────────────────────────────────────────────────────

    def fetch_currency(
        self,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.1 — Fetch the store's active currency."""
        ...

    def list_attributes(
        self,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.2 — Fetch all product attributes and their terms (custom-plugin surface)."""
        ...

    def list_categories(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 2.3 — List all product categories."""
        ...

    def list_tags(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 2.4 — List all product tags."""
        ...

    def list_published_products(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.5 — List all published products."""
        ...

    # ── Products ────────────────────────────────────────────────────────────

    def fetch_product(
        self,
        product_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.1 — Fetch a single product by ID."""
        ...

    def fetch_variant(
        self,
        product_id: int,
        variant_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.2 — Fetch a single variation/variant by product and variant ID."""
        ...

    def list_variants(
        self,
        product_id: int,
        page: int = 1,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 4.3 — List all variants/variations for a product."""
        ...

    def products_advanced(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.4 — Advanced product search via custom plugin (custom_plugin surface)."""
        ...

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
        """Row 5.1 — List orders for a customer."""
        ...

    def fetch_order(
        self,
        order_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.2 — Fetch a single order by ID."""
        ...

    def check_stock(
        self,
        product_ids: List[int],
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.3 — Check stock status for a list of product IDs (custom_plugin surface)."""
        ...

    def create_order(
        self,
        payload: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.4 — Create a new order."""
        ...

    def historical_product_search(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.5 — Search products from past order IDs (custom_plugin surface)."""
        ...

    # ── Customers ───────────────────────────────────────────────────────────

    def fetch_customer(
        self,
        customer_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Rows 6.1 & 6.2 — Fetch a customer by ID."""
        ...

    def update_customer(
        self,
        customer_id: int,
        payload: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 6.3 — Update a customer's profile."""
        ...

    # ── Additional (not in CSV mapping; present in codebase) ────────────────

    def list_coupons(
        self,
        page: int,
        per_page: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List available coupon codes (COUPON_INQUIRY intent)."""
        ...

    def fetch_wishlist(
        self,
        customer_id,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Fetch customer wishlist (SAVE_FOR_LATER / WISHLIST intent)."""
        ...

    def list_products_on_sale(
        self,
        page: int,
        per_page: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List products currently on sale (DISCOUNT_INQUIRY intent)."""
        ...

    def list_attribute_terms(
        self,
        attribute_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List terms for a product attribute (PRODUCT_TYPES intent)."""
        ...

    def search_products(
        self,
        search_term: str,
        page: int,
        per_page: int,
        status: str = "publish",
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Text search fallback for products (QUICK_ORDER / PRODUCT_SEARCH intents)."""
        ...

    def list_cs_orders(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List orders via custom plugin (CS-rep role path; custom_plugin surface)."""
        ...
