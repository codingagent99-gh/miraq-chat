"""
test_ecommerce_normalization.py

Unit tests for the Phase-3 parser methods added to WooEndpoints.

Covers:
  1. parse_product  — price fallback chain + in_stock + _raw
  2. parse_variant  — price + options (list and dict formats) + in_stock + _raw
  3. parse_list_variants — list delegation + empty / bad input
  4. parse_order    — status + billing_address + shipping_address + _raw
  5. parse_customer — default_address + addresses dedup + _raw
  6. parse_list_published_products — in_stock + price + _raw + bad input
"""

import pytest

from ecommerce.woo_endpoints import WooEndpoints, _normalize_woo_address

# ── Shared fixture ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ep():
    return WooEndpoints()


# ════════════════════════════════════════════════════════════════════════════
# _normalize_woo_address helper
# ════════════════════════════════════════════════════════════════════════════

class TestNormalizeWooAddress:
    def test_full_address(self):
        addr = {
            "address_1": "123 Main St",
            "address_2": "Suite 4",
            "city": "Springfield",
            "state": "IL",
            "postcode": "62701",
            "country": "US",
        }
        result = _normalize_woo_address(addr)
        assert result == addr

    def test_missing_fields_default_to_empty_string(self):
        result = _normalize_woo_address({})
        assert result == {
            "address_1": "",
            "address_2": "",
            "city": "",
            "state": "",
            "postcode": "",
            "country": "",
        }

    def test_extra_woo_fields_are_dropped(self):
        """WooCommerce billing dict has first_name, email, phone — these are NOT in the neutral shape."""
        addr = {
            "address_1": "42 Elm St",
            "city": "Shelbyville",
            "postcode": "12345",
            "country": "US",
            "first_name": "Homer",
            "email": "homer@example.com",
        }
        result = _normalize_woo_address(addr)
        assert "first_name" not in result
        assert "email" not in result
        assert result["address_1"] == "42 Elm St"


# ════════════════════════════════════════════════════════════════════════════
# 1. parse_product
# ════════════════════════════════════════════════════════════════════════════

class TestParseProduct:
    WOO_PRODUCT = {
        "id": 10,
        "name": "Mosaic Tile",
        "sale_price": "8.99",
        "price": "8.99",
        "regular_price": "12.00",
        "stock_status": "instock",
    }

    def test_typical_response(self, ep):
        parsed = ep.parse_product(self.WOO_PRODUCT)
        assert parsed["id"] == 10
        assert parsed["price"] == "8.99"   # sale_price wins
        assert parsed["in_stock"] is True

    def test_raw_key_present_and_equals_input(self, ep):
        parsed = ep.parse_product(self.WOO_PRODUCT)
        assert "_raw" in parsed
        assert parsed["_raw"] is self.WOO_PRODUCT

    def test_price_fallback_no_sale_price(self, ep):
        product = {"id": 1, "price": "15.00", "regular_price": "15.00", "stock_status": "instock"}
        parsed = ep.parse_product(product)
        assert parsed["price"] == "15.00"

    def test_price_fallback_only_regular_price(self, ep):
        product = {"id": 1, "regular_price": "20.00", "stock_status": "instock"}
        parsed = ep.parse_product(product)
        assert parsed["price"] == "20.00"

    def test_price_empty_when_no_price_fields(self, ep):
        parsed = ep.parse_product({"id": 1, "stock_status": "instock"})
        assert parsed["price"] == ""

    def test_in_stock_false_when_outofstock(self, ep):
        product = {**self.WOO_PRODUCT, "stock_status": "outofstock"}
        assert ep.parse_product(product)["in_stock"] is False

    def test_in_stock_false_when_stock_status_missing(self, ep):
        assert ep.parse_product({"id": 1})["in_stock"] is False

    def test_empty_response(self, ep):
        parsed = ep.parse_product({})
        assert parsed["id"] is None
        assert parsed["price"] == ""
        assert parsed["in_stock"] is False
        assert parsed["_raw"] == {}


# ════════════════════════════════════════════════════════════════════════════
# 2. parse_variant
# ════════════════════════════════════════════════════════════════════════════

class TestParseVariant:
    WOO_VARIATION = {
        "id": 55,
        "sale_price": "9.50",
        "price": "9.50",
        "regular_price": "12.00",
        "stock_status": "instock",
        "attributes": [
            {"id": 1, "name": "Color", "option": "Red"},
            {"id": 2, "name": "Size", "option": "Large"},
        ],
    }

    def test_typical_response(self, ep):
        parsed = ep.parse_variant(self.WOO_VARIATION)
        assert parsed["id"] == 55
        assert parsed["price"] == "9.50"
        assert parsed["in_stock"] is True
        assert parsed["options"] == {"Color": "Red", "Size": "Large"}

    def test_raw_key_present_and_equals_input(self, ep):
        parsed = ep.parse_variant(self.WOO_VARIATION)
        assert "_raw" in parsed
        assert parsed["_raw"] is self.WOO_VARIATION

    def test_price_fallback_no_sale_price(self, ep):
        var = {"id": 1, "price": "11.00", "regular_price": "11.00", "stock_status": "instock", "attributes": []}
        assert ep.parse_variant(var)["price"] == "11.00"

    def test_price_fallback_sale_price_empty_string(self, ep):
        """Empty sale_price (not on sale) should fall through to price."""
        var = {"id": 1, "sale_price": "", "price": "11.00", "regular_price": "11.00", "stock_status": "instock", "attributes": []}
        assert ep.parse_variant(var)["price"] == "11.00"

    def test_price_empty_when_no_price_fields(self, ep):
        parsed = ep.parse_variant({"id": 1, "stock_status": "instock"})
        assert parsed["price"] == ""

    def test_in_stock_false_when_outofstock(self, ep):
        var = {**self.WOO_VARIATION, "stock_status": "outofstock"}
        assert ep.parse_variant(var)["in_stock"] is False

    def test_options_dict_format(self, ep):
        """Custom-plugin variations carry attributes as a flat dict."""
        var = {
            "id": 99,
            "price": "5.00",
            "stock_status": "instock",
            "attributes": {"Color": "Blue", "Finish": "Matte"},
        }
        parsed = ep.parse_variant(var)
        assert parsed["options"] == {"Color": "Blue", "Finish": "Matte"}

    def test_options_empty_when_no_attributes(self, ep):
        parsed = ep.parse_variant({"id": 1, "price": "5.00", "stock_status": "instock"})
        assert parsed["options"] == {}

    def test_options_skips_entries_missing_name_or_option(self, ep):
        var = {
            "id": 1,
            "price": "5.00",
            "stock_status": "instock",
            "attributes": [
                {"name": "Color", "option": ""},   # no option → skipped
                {"name": "", "option": "Red"},      # no name → skipped
                {"name": "Size", "option": "M"},    # valid
            ],
        }
        assert ep.parse_variant(var)["options"] == {"Size": "M"}

    def test_empty_response(self, ep):
        parsed = ep.parse_variant({})
        assert parsed["id"] is None
        assert parsed["price"] == ""
        assert parsed["options"] == {}
        assert parsed["in_stock"] is False
        assert parsed["_raw"] == {}


# ════════════════════════════════════════════════════════════════════════════
# 3. parse_list_variants
# ════════════════════════════════════════════════════════════════════════════

class TestParseListVariants:
    def test_typical_list(self, ep):
        variations = [
            {"id": 1, "price": "5.00", "stock_status": "instock", "attributes": [{"name": "Color", "option": "Red"}]},
            {"id": 2, "price": "6.00", "stock_status": "outofstock", "attributes": [{"name": "Color", "option": "Blue"}]},
        ]
        parsed = ep.parse_list_variants(variations)
        assert len(parsed) == 2
        assert parsed[0]["id"] == 1
        assert parsed[0]["in_stock"] is True
        assert parsed[0]["options"] == {"Color": "Red"}
        assert parsed[1]["in_stock"] is False

    def test_raw_key_present_per_item(self, ep):
        variations = [{"id": 1, "price": "5.00", "stock_status": "instock", "attributes": []}]
        parsed = ep.parse_list_variants(variations)
        assert parsed[0]["_raw"] is variations[0]

    def test_empty_list(self, ep):
        assert ep.parse_list_variants([]) == []

    def test_non_dict_items_skipped(self, ep):
        assert ep.parse_list_variants([None, "bad", 42]) == []

    def test_non_list_returns_empty(self, ep):
        assert ep.parse_list_variants(None) == []
        assert ep.parse_list_variants({}) == []


# ════════════════════════════════════════════════════════════════════════════
# 4. parse_order
# ════════════════════════════════════════════════════════════════════════════

class TestParseOrder:
    WOO_ORDER = {
        "id": 100,
        "status": "processing",
        "billing": {
            "address_1": "10 Downing St",
            "address_2": "",
            "city": "London",
            "state": "England",
            "postcode": "SW1A 2AA",
            "country": "GB",
            "first_name": "John",
            "email": "pm@example.com",
        },
        "shipping": {
            "address_1": "15 Baker St",
            "address_2": "",
            "city": "London",
            "state": "England",
            "postcode": "NW1 6XE",
            "country": "GB",
        },
    }

    def test_typical_response(self, ep):
        parsed = ep.parse_order(self.WOO_ORDER)
        assert parsed["id"] == 100
        assert parsed["status"] == "processing"
        assert parsed["billing_address"]["address_1"] == "10 Downing St"
        assert parsed["billing_address"]["city"] == "London"
        assert parsed["shipping_address"]["address_1"] == "15 Baker St"

    def test_raw_key_present_and_equals_input(self, ep):
        parsed = ep.parse_order(self.WOO_ORDER)
        assert "_raw" in parsed
        assert parsed["_raw"] is self.WOO_ORDER

    def test_raw_billing_retains_extra_fields(self, ep):
        """WooCommerce billing has first_name, email, etc. — accessible via _raw."""
        parsed = ep.parse_order(self.WOO_ORDER)
        assert parsed["_raw"]["billing"]["first_name"] == "John"
        assert parsed["_raw"]["billing"]["email"] == "pm@example.com"

    def test_billing_address_neutral_shape_no_extra_fields(self, ep):
        """Neutral billing_address must NOT include first_name, email, etc."""
        parsed = ep.parse_order(self.WOO_ORDER)
        assert "first_name" not in parsed["billing_address"]
        assert "email" not in parsed["billing_address"]

    def test_missing_billing_defaults_to_empty_address(self, ep):
        parsed = ep.parse_order({"id": 1, "status": "pending"})
        assert parsed["billing_address"] == {
            "address_1": "", "address_2": "", "city": "",
            "state": "", "postcode": "", "country": "",
        }

    def test_empty_response(self, ep):
        parsed = ep.parse_order({})
        assert parsed["id"] is None
        assert parsed["status"] == ""
        assert parsed["_raw"] == {}


# ════════════════════════════════════════════════════════════════════════════
# 5. parse_customer
# ════════════════════════════════════════════════════════════════════════════

class TestParseCustomer:
    WOO_CUSTOMER = {
        "id": 42,
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
        "billing": {
            "address_1": "100 Main St",
            "address_2": "",
            "city": "Austin",
            "state": "TX",
            "postcode": "78701",
            "country": "US",
            "first_name": "Jane",
            "email": "jane@example.com",
        },
        "shipping": {
            "address_1": "200 Oak Ave",
            "address_2": "Apt 5",
            "city": "Austin",
            "state": "TX",
            "postcode": "78702",
            "country": "US",
        },
    }

    def test_typical_response(self, ep):
        parsed = ep.parse_customer(self.WOO_CUSTOMER)
        assert parsed["id"] == 42
        assert parsed["first_name"] == "Jane"
        assert parsed["last_name"] == "Doe"
        assert parsed["email"] == "jane@example.com"
        assert parsed["default_address"]["address_1"] == "100 Main St"

    def test_raw_key_present_and_equals_input(self, ep):
        parsed = ep.parse_customer(self.WOO_CUSTOMER)
        assert "_raw" in parsed
        assert parsed["_raw"] is self.WOO_CUSTOMER

    def test_addresses_contains_both_when_different(self, ep):
        parsed = ep.parse_customer(self.WOO_CUSTOMER)
        assert len(parsed["addresses"]) == 2
        assert parsed["addresses"][0]["address_1"] == "100 Main St"
        assert parsed["addresses"][1]["address_1"] == "200 Oak Ave"

    def test_addresses_deduped_when_billing_equals_shipping(self, ep):
        customer = dict(self.WOO_CUSTOMER)
        same_addr = {
            "address_1": "100 Main St", "address_2": "",
            "city": "Austin", "state": "TX",
            "postcode": "78701", "country": "US",
        }
        customer = {**customer, "billing": same_addr, "shipping": same_addr}
        parsed = ep.parse_customer(customer)
        assert len(parsed["addresses"]) == 1

    def test_addresses_deduped_when_shipping_empty(self, ep):
        customer = {**self.WOO_CUSTOMER, "shipping": {}}
        parsed = ep.parse_customer(customer)
        # Shipping is empty — only billing should appear
        assert len(parsed["addresses"]) == 1

    def test_empty_response(self, ep):
        parsed = ep.parse_customer({})
        assert parsed["id"] is None
        assert parsed["first_name"] == ""
        assert parsed["email"] == ""
        assert parsed["default_address"]["address_1"] == ""
        assert parsed["addresses"] == [parsed["default_address"]]
        assert parsed["_raw"] == {}


# ════════════════════════════════════════════════════════════════════════════
# 6. parse_list_published_products
# ════════════════════════════════════════════════════════════════════════════

class TestParseListPublishedProducts:
    def test_typical_list(self, ep):
        products = [
            {"id": 1, "sale_price": "10.00", "price": "10.00", "regular_price": "12.00", "stock_status": "instock"},
            {"id": 2, "price": "5.00", "regular_price": "5.00", "stock_status": "outofstock"},
        ]
        parsed = ep.parse_list_published_products(products)
        assert len(parsed) == 2
        assert parsed[0]["id"] == 1
        assert parsed[0]["price"] == "10.00"
        assert parsed[0]["in_stock"] is True
        assert parsed[1]["in_stock"] is False

    def test_raw_key_present_per_item(self, ep):
        products = [{"id": 1, "price": "5.00", "stock_status": "instock"}]
        parsed = ep.parse_list_published_products(products)
        assert parsed[0]["_raw"] is products[0]

    def test_price_fallback_no_sale_price(self, ep):
        products = [{"id": 1, "price": "7.00", "regular_price": "7.00", "stock_status": "instock"}]
        parsed = ep.parse_list_published_products(products)
        assert parsed[0]["price"] == "7.00"

    def test_price_empty_when_no_price_fields(self, ep):
        products = [{"id": 1, "stock_status": "instock"}]
        parsed = ep.parse_list_published_products(products)
        assert parsed[0]["price"] == ""

    def test_empty_list(self, ep):
        assert ep.parse_list_published_products([]) == []

    def test_non_dict_items_skipped(self, ep):
        assert ep.parse_list_published_products([None, 42, "bad"]) == []

    def test_non_list_input_returns_empty(self, ep):
        assert ep.parse_list_published_products(None) == []
        assert ep.parse_list_published_products({}) == []
