from ecommerce.woo_adapters import (
    normalize_product,
    normalize_variant,
    normalize_order,
    normalize_customer,
    normalize_response,
)


def test_normalize_product_prefers_sale_price_and_builds_options():
    raw = {
        "id": 101,
        "name": "Aura Tile",
        "type": "variable",
        "price": "20.00",
        "regular_price": "25.00",
        "sale_price": "18.00",
        "stock_status": "instock",
        "stock_quantity": 12,
        "attributes": [
            {"name": "Color", "options": ["Blue", "Green"], "variation": True},
        ],
        "variations": [11, 12],
    }

    product = normalize_product(raw)

    assert product["price"] == "18.00"
    assert product["original_price"] == "25.00"
    assert product["in_stock"] is True
    assert product["variant_ids"] == [11, 12]
    assert product["options"] == [{"name": "Color", "values": ["Blue", "Green"], "options": ["Blue", "Green"], "is_variation": True}]
    assert product["_raw"]["sale_price"] == "18.00"


def test_normalize_variant_builds_option_map_and_stock_flag():
    raw = {
        "id": 55,
        "price": "29.99",
        "stock_status": "instock",
        "attributes": [
            {"name": "Color", "option": "Blue"},
            {"name": "Size", "option": "M"},
        ],
    }

    variant = normalize_variant(raw)

    assert variant["price"] == "29.99"
    assert variant["in_stock"] is True
    assert variant["options"] == {"Color": "Blue", "Size": "M"}
    assert variant["attributes"] == [{"name": "Color", "value": "Blue"}, {"name": "Size", "value": "M"}]
    assert variant["_raw"]["attributes"][0]["option"] == "Blue"


def test_normalize_order_maps_addresses_and_line_items():
    raw = {
        "id": 900,
        "number": "900",
        "status": "processing",
        "currency_symbol": "$",
        "total": "44.00",
        "payment_method_title": "Cash on Delivery",
        "date_created": "2026-05-01T12:00:00",
        "date_paid": None,
        "billing": {"first_name": "Ada", "address_1": "1 Main", "city": "Boston", "postcode": "02110", "country": "US", "phone": "123"},
        "shipping": {"first_name": "Ada", "address_1": "1 Main", "city": "Boston", "postcode": "02110", "country": "US"},
        "line_items": [{"name": "Aura Tile", "quantity": 2, "price": "22", "total": "44", "sku": "AURA", "product_id": 101, "variation_id": 55}],
    }

    order = normalize_order(raw)

    assert order["payment_method_label"] == "Cash on Delivery"
    assert order["created_at"] == "2026-05-01T12:00:00"
    assert order["shipping_address"]["address_1"] == "1 Main"
    assert order["line_items"][0]["variant_id"] == 55
    assert order["_raw"]["billing"]["address_1"] == "1 Main"


def test_normalize_customer_prefers_shipping_as_default_and_dedupes_addresses():
    raw = {
        "id": 77,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "billing": {"address_1": "1 Main", "city": "Boston", "postcode": "02110", "country": "US", "phone": "123"},
        "shipping": {"address_1": "2 Side", "city": "Boston", "postcode": "02111", "country": "US"},
    }

    customer = normalize_customer(raw)

    assert customer["default_address"]["address_1"] == "2 Side"
    assert len(customer["addresses"]) == 2
    assert customer["_raw"]["email"] == "ada@example.com"


def test_products_advanced_response_normalizes_embedded_products():
    raw = {
        "products": [
            {
                "id": 101,
                "name": "Aura Tile",
                "price": "20.00",
                "stock_status": "instock",
                "attributes": [{"name": "Color", "options": ["Blue"], "variation": True}],
                "variations": [{"id": 55, "price": "20.00", "stock_status": "instock", "attributes": [{"name": "Color", "option": "Blue"}]}],
            }
        ],
        "total": 1,
        "pages": 1,
    }

    normalized, total, pages = normalize_response("products_advanced", raw, {})

    assert total == "1"
    assert pages == "1"
    assert normalized["products"][0]["variations"][0]["options"] == {"Color": "Blue"}
    assert normalized["_raw"]["total"] == 1
