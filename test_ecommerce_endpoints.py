"""
test_ecommerce_endpoints.py

Smoke tests for the ecommerce/ package introduced in Phases 1 & 2.

Covers:
  1. get_endpoints() factory — returns WooEndpoints for "woocommerce", raises on unknown.
  2. Each endpoints.xxx() function returns a WooAPICall with the expected
     method, relative endpoint path, and surface.
  3. WooAPICall.surface field exists with default "admin".
  4. surface="custom_plugin" functions return the correct surface.
"""

import os
import pytest

from models import WooAPICall


# ════════════════════════════════════════════════════════════════════════════
# 1. Factory tests
# ════════════════════════════════════════════════════════════════════════════

class TestGetEndpoints:
    def test_returns_woo_endpoints_when_backend_unset(self, monkeypatch):
        monkeypatch.delenv("ECOMMERCE_BACKEND", raising=False)
        from ecommerce.woo_endpoints import WooEndpoints
        # Re-import to pick up env change
        from ecommerce import get_endpoints
        result = get_endpoints()
        assert isinstance(result, WooEndpoints)

    def test_returns_woo_endpoints_when_backend_is_woocommerce(self, monkeypatch):
        monkeypatch.setenv("ECOMMERCE_BACKEND", "woocommerce")
        from ecommerce.woo_endpoints import WooEndpoints
        from ecommerce import get_endpoints
        result = get_endpoints()
        assert isinstance(result, WooEndpoints)

    def test_raises_on_unknown_backend(self, monkeypatch):
        monkeypatch.setenv("ECOMMERCE_BACKEND", "unknown_backend")
        from ecommerce import get_endpoints
        with pytest.raises(ValueError, match="Unknown ECOMMERCE_BACKEND"):
            get_endpoints()

    def test_raises_on_shopify_backend(self, monkeypatch):
        """Shopify is not yet implemented — must raise ValueError."""
        monkeypatch.setenv("ECOMMERCE_BACKEND", "shopify")
        from ecommerce import get_endpoints
        with pytest.raises(ValueError, match="Unknown ECOMMERCE_BACKEND"):
            get_endpoints()


# ════════════════════════════════════════════════════════════════════════════
# 2. WooAPICall.surface field
# ════════════════════════════════════════════════════════════════════════════

class TestWooAPICallSurface:
    def test_default_surface_is_admin(self):
        call = WooAPICall(method="GET", endpoint="/products/1")
        assert call.surface == "admin"

    def test_custom_plugin_surface(self):
        call = WooAPICall(method="POST", endpoint="/products-advanced-new", surface="custom_plugin")
        assert call.surface == "custom_plugin"

    def test_no_is_custom_api_field(self):
        """is_custom_api must be gone — accessing it should raise AttributeError."""
        call = WooAPICall(method="GET", endpoint="/orders")
        assert not hasattr(call, "is_custom_api")


# ════════════════════════════════════════════════════════════════════════════
# 3. Endpoint function smoke tests — method / path / surface
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def ep():
    """Module-scoped WooEndpoints instance."""
    from ecommerce.woo_endpoints import WooEndpoints
    return WooEndpoints()


class TestStoreMetadata:
    def test_fetch_currency(self, ep):
        c = ep.fetch_currency()
        assert isinstance(c, WooAPICall)
        assert c.method == "GET"
        assert c.endpoint == "/data/currencies/current"
        assert c.surface == "admin"

    def test_list_attributes(self, ep):
        c = ep.list_attributes()
        assert c.method == "GET"
        assert c.endpoint == "/all-attributes"
        assert c.surface == "custom_plugin"

    def test_list_categories(self, ep):
        c = ep.list_categories(page=1, per_page=50)
        assert c.method == "GET"
        assert c.endpoint == "/products/categories"
        assert c.surface == "admin"
        assert c.params["page"] == 1
        assert c.params["per_page"] == 50

    def test_list_tags(self, ep):
        c = ep.list_tags(page=2)
        assert c.method == "GET"
        assert c.endpoint == "/products/tags"
        assert c.surface == "admin"
        assert c.params["page"] == 2

    def test_list_published_products(self, ep):
        c = ep.list_published_products(page=1)
        assert c.method == "GET"
        assert c.endpoint == "/products"
        assert c.params["status"] == "publish"
        assert c.surface == "admin"


class TestProducts:
    def test_fetch_product(self, ep):
        c = ep.fetch_product(product_id=42)
        assert c.method == "GET"
        assert c.endpoint == "/products/42"
        assert c.surface == "admin"

    def test_fetch_variant(self, ep):
        c = ep.fetch_variant(product_id=10, variant_id=55)
        assert c.method == "GET"
        assert c.endpoint == "/products/10/variations/55"
        assert c.surface == "admin"

    def test_list_variants(self, ep):
        c = ep.list_variants(product_id=10, page=1, per_page=100)
        assert c.method == "GET"
        assert c.endpoint == "/products/10/variations"
        assert c.params["per_page"] == 100
        assert c.surface == "admin"

    def test_products_advanced(self, ep):
        body = {"ids": [1, 2, 3]}
        c = ep.products_advanced(body=body)
        assert c.method == "POST"
        assert c.endpoint == "/products-advanced-new"
        assert c.surface == "custom_plugin"
        assert c.body == body


class TestOrders:
    def test_list_customer_orders(self, ep):
        c = ep.list_customer_orders(customer_id=7, page=1, per_page=5)
        assert c.method == "GET"
        assert c.endpoint == "/orders"
        assert c.params["customer"] == 7
        assert c.params["per_page"] == 5
        assert c.surface == "admin"

    def test_list_customer_orders_extra_filters(self, ep):
        c = ep.list_customer_orders(customer_id=7, page=1, after="2025-01-01")
        assert c.params["after"] == "2025-01-01"

    def test_fetch_order(self, ep):
        c = ep.fetch_order(order_id=99)
        assert c.method == "GET"
        assert c.endpoint == "/orders/99"
        assert c.surface == "admin"

    def test_check_stock_delegates_to_products_advanced(self, ep):
        c = ep.check_stock(product_ids=[1, 2, 3])
        assert c.method == "POST"
        assert c.endpoint == "/products-advanced-new"
        assert c.surface == "custom_plugin"
        assert c.body["ids"] == [1, 2, 3]
        assert c.body["per_page"] == 3

    def test_create_order(self, ep):
        payload = {"customer_id": 7, "line_items": []}
        c = ep.create_order(payload=payload)
        assert c.method == "POST"
        assert c.endpoint == "/orders"
        assert c.body == payload
        assert c.surface == "admin"

    def test_historical_product_search_delegates_to_products_advanced(self, ep):
        body = {"ids": [10], "per_page": 1}
        c = ep.historical_product_search(body=body)
        assert c.method == "POST"
        assert c.endpoint == "/products-advanced-new"
        assert c.surface == "custom_plugin"
        assert c.body == body

    def test_list_cs_orders(self, ep):
        body = {"customer_id": 5, "page": 1, "per_page": 10}
        c = ep.list_cs_orders(body=body)
        assert c.method == "POST"
        assert c.endpoint == "/orders"
        assert c.surface == "custom_plugin"
        assert c.body == body


class TestCustomers:
    def test_fetch_customer(self, ep):
        c = ep.fetch_customer(customer_id=42)
        assert c.method == "GET"
        assert c.endpoint == "/customers/42"
        assert c.surface == "admin"

    def test_update_customer(self, ep):
        payload = {"billing": {"address_1": "123 Main St"}}
        c = ep.update_customer(customer_id=42, payload=payload)
        assert c.method == "PUT"
        assert c.endpoint == "/customers/42"
        assert c.body == payload
        assert c.surface == "admin"


class TestAdditional:
    def test_list_coupons(self, ep):
        c = ep.list_coupons(page=1, per_page=10)
        assert c.method == "GET"
        assert c.endpoint == "/coupons"
        assert c.surface == "admin"

    def test_fetch_wishlist(self, ep):
        c = ep.fetch_wishlist(customer_id="CURRENT_USER")
        assert c.method == "POST"
        assert c.endpoint == "/wishlist"
        assert c.params["customer_id"] == "CURRENT_USER"
        assert c.surface == "admin"

    def test_list_products_on_sale(self, ep):
        c = ep.list_products_on_sale(page=1, per_page=20)
        assert c.method == "GET"
        assert c.endpoint == "/products"
        assert c.params["on_sale"] == "true"
        assert c.surface == "admin"

    def test_list_attribute_terms(self, ep):
        c = ep.list_attribute_terms(attribute_id=5)
        assert c.method == "GET"
        assert c.endpoint == "/products/attributes/5/terms"
        assert c.surface == "admin"

    def test_search_products(self, ep):
        c = ep.search_products(search_term="blue tile", page=1, per_page=20)
        assert c.method == "GET"
        assert c.endpoint == "/products"
        assert c.params["search"] == "blue tile"
        assert c.params["status"] == "publish"
        assert c.surface == "admin"


# ════════════════════════════════════════════════════════════════════════════
# 4. description and requires_resolution optional params
# ════════════════════════════════════════════════════════════════════════════

class TestOptionalParams:
    def test_custom_description(self, ep):
        c = ep.fetch_product(product_id=1, description="My custom description")
        assert c.description == "My custom description"

    def test_default_description_non_empty(self, ep):
        c = ep.fetch_product(product_id=1)
        assert c.description  # should have a non-empty default

    def test_requires_resolution_passthrough(self, ep):
        c = ep.list_customer_orders(
            customer_id="CURRENT_USER_ID",
            page=1,
            requires_resolution=["customer_id"],
        )
        assert c.requires_resolution == ["customer_id"]

    def test_requires_resolution_defaults_to_empty_list(self, ep):
        c = ep.fetch_product(product_id=1)
        assert c.requires_resolution == []
