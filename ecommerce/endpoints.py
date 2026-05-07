"""
ecommerce/endpoints.py — Protocol declaring the interface every e-commerce
backend module must implement.

Each ``fetch_*`` / ``list_*`` function returns a ``WooAPICall`` ready to be
passed to ``woo_client.execute()``.  Each is paired with a ``parse_*`` method
that normalizes the raw response from ``woo_client.execute()`` into a
backend-neutral dict so callers remain portable across WooCommerce, Shopify,
and any future backend.

All parsers include a ``_raw`` key with the original response as a migration
safety valve — callers can read any not-yet-normalized backend-specific field
from ``_raw[...]``.

A future ``shopify_endpoints.py`` is a drop-in if it provides a class that
satisfies this Protocol.
"""

from typing import Dict, List, Optional, Protocol

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

    def build_cart_variation_payload(
        self,
        *,
        product_id: int,
        variant_id: Optional[int],
        resolved_attrs: dict[str, str],
        store_loader,
    ) -> dict:
        """Build backend-specific cart-line payload for add-to-cart actions."""
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

    # ── Response parsers ────────────────────────────────────────────────────
    # Each parser takes the raw ``woo_client.execute(...).get("data")`` value
    # and returns a backend-neutral dict (or list of dicts).  Every result
    # includes a ``_raw`` key with the original response so callers can access
    # any not-yet-normalized backend-specific field.

    def parse_product(self, response: dict) -> dict:
        """Normalise a single product response into a backend-neutral dict.

        Returns at minimum: ``{"id", "price": str, "in_stock": bool, "_raw"}``.
        """
        ...

    def parse_variant(self, response: dict) -> dict:
        """Normalise a single variant/variation response into a backend-neutral dict.

        Returns at minimum:
        ``{"id", "price": str, "options": dict[str, str], "in_stock": bool, "_raw"}``.
        """
        ...

    def parse_list_variants(self, response: list) -> List[dict]:
        """Normalise a variants/variations list into backend-neutral dicts.

        Returns a list where each item is in the same shape as ``parse_variant``.
        """
        ...

    def parse_order(self, response: dict) -> dict:
        """Normalise a single order response into a backend-neutral dict.

        Returns at minimum:
        ``{"id", "status": str, "billing_address": dict, "shipping_address": dict, "_raw"}``.
        Address dicts use the neutral six-key shape:
        ``{"address_1", "address_2", "city", "state", "postcode", "country"}``.
        """
        ...

    def parse_customer(self, response: dict) -> dict:
        """Normalise a single customer response into a backend-neutral dict.

        Returns at minimum:
        ``{"id", "first_name", "last_name", "email",
           "default_address": dict, "addresses": list[dict], "_raw"}``.
        Address dicts use the neutral six-key shape.
        """
        ...

    def parse_list_published_products(self, response: list) -> List[dict]:
        """Normalise a published-products list into backend-neutral dicts.

        Returns a list where each item contains at minimum:
        ``{"id", "price": str, "in_stock": bool, "_raw"}``.
        """
        ...
