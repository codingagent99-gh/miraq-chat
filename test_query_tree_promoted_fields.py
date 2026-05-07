"""
test_query_tree_promoted_fields.py — Tests for the three new special-field
leaf node constructors and their routing in serialize_query.

Phase 4b.9: make_price_condition, make_stock_condition, make_search_condition
"""

import json
import pytest

from api_builder.query_tree import (
    make_condition,
    make_or_group,
    make_price_condition,
    make_stock_condition,
    make_search_condition,
    serialize_query,
)


# ─── Constructor shape tests ───

def test_make_price_condition_both_bounds():
    node = make_price_condition(min_price=10.0, max_price=50.0)
    assert node == {"field_type": "price", "min": 10.0, "max": 50.0}


def test_make_price_condition_min_only():
    node = make_price_condition(min_price=5.0)
    assert node == {"field_type": "price", "min": 5.0, "max": None}


def test_make_price_condition_max_only():
    node = make_price_condition(max_price=100.0)
    assert node == {"field_type": "price", "min": None, "max": 100.0}


def test_make_price_condition_no_bounds():
    node = make_price_condition()
    assert node == {"field_type": "price", "min": None, "max": None}


def test_make_stock_condition():
    node = make_stock_condition("instock")
    assert node == {"field_type": "stock_status", "value": "instock"}


def test_make_search_condition():
    node = make_search_condition("blue mosaic")
    assert node == {"field_type": "search", "value": "blue mosaic"}


# ─── serialize_query routing tests ───

def test_serialize_query_price_node_both_bounds():
    body = serialize_query([make_price_condition(10.0, 50.0)], page=1, per_page=24)
    assert body["price"] == {"min": 10.0, "max": 50.0}
    assert "filters" not in body


def test_serialize_query_price_node_min_only():
    body = serialize_query([make_price_condition(min_price=5.0)], page=1, per_page=24)
    assert body["price"] == {"min": 5.0}
    assert "max" not in body["price"]


def test_serialize_query_price_node_max_only():
    body = serialize_query([make_price_condition(max_price=100.0)], page=1, per_page=24)
    assert body["price"] == {"max": 100.0}
    assert "min" not in body["price"]


def test_serialize_query_empty_price_node_omits_price_key():
    """A price node with both None should not add body['price'] at all."""
    body = serialize_query([make_price_condition()], page=1, per_page=24)
    assert "price" not in body


def test_serialize_query_stock_node():
    body = serialize_query([make_stock_condition("instock")], page=1, per_page=24)
    assert body["stock_status"] == "instock"
    assert "filters" not in body


def test_serialize_query_search_node():
    body = serialize_query([make_search_condition("mosaic")], page=1, per_page=24)
    assert body["search"] == "mosaic"
    assert "filters" not in body


def test_serialize_query_taxonomy_condition_goes_to_filters():
    cond = make_condition("product_tag", ["quick-ship"], "IN")
    body = serialize_query([cond], page=1, per_page=24)
    assert "filters" in body
    assert body["filters"]["relation"] == "AND"
    assert body["filters"]["conditions"][0]["taxonomy"] == "product_tag"


def test_serialize_query_mixed_special_and_taxonomy():
    """Price and stock nodes + taxonomy conditions all coexist correctly."""
    conditions = [
        make_price_condition(min_price=10.0, max_price=50.0),
        make_stock_condition("instock"),
        make_condition("pa_color", ["red"], "IN"),
        make_condition("product_tag", ["sale"], "IN"),
    ]
    body = serialize_query(conditions, page=2, per_page=12)

    assert body["page"] == 2
    assert body["per_page"] == 12
    assert body["price"] == {"min": 10.0, "max": 50.0}
    assert body["stock_status"] == "instock"
    assert "filters" in body
    tax_conditions = body["filters"]["conditions"]
    assert len(tax_conditions) == 2
    taxonomies = {c["taxonomy"] for c in tax_conditions}
    assert taxonomies == {"pa_color", "product_tag"}


def test_serialize_query_no_conditions():
    body = serialize_query([], page=1, per_page=24)
    assert body == {"page": 1, "per_page": 24}
    assert "filters" not in body
    assert "price" not in body
    assert "stock_status" not in body
    assert "search" not in body


def test_serialize_query_page_per_page_always_present():
    body = serialize_query([make_stock_condition("outofstock")], page=3, per_page=50)
    assert body["page"] == 3
    assert body["per_page"] == 50


def test_serialize_query_output_is_json_serializable():
    """The body must be JSON-serializable (no custom objects)."""
    conditions = [
        make_price_condition(1.0, 999.0),
        make_stock_condition("instock"),
        make_search_condition("tile"),
        make_condition("pa_finish", ["matte"], "IN"),
    ]
    body = serialize_query(conditions, page=1, per_page=24)
    # Should not raise
    json.dumps(body)
