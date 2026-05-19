"""
test_filter_builder_byte_identical.py — Regression tests asserting byte-identical
Woo plugin JSON bodies before and after the Phase 4b.9 refactor.

For each representative query, the expected body JSON is captured here.
The test asserts the live output matches exactly.
"""

import json
from unittest.mock import patch

import pytest

from api_builder.filter_builder import build_advanced_filter_call
from models.catalog import CatalogAttribute, CatalogAttributeTerm, CatalogCategory, CatalogTag


class _FakeLoader:
    """Minimal loader stub with neutral-key data for representative queries."""

    def __init__(self):
        self._color = CatalogAttribute(
            key="color",
            label="Color",
            terms=(
                CatalogAttributeTerm(key="red", name="Red", backend_ref={"slug": "red"}),
                CatalogAttributeTerm(key="blue", name="Blue", backend_ref={"slug": "blue"}),
            ),
            backend_ref={"taxonomy": "pa_color"},
        )
        self._size = CatalogAttribute(
            key="tile-size",
            label="Tile Size",
            terms=(
                CatalogAttributeTerm(key="4x4", name="4x4", backend_ref={"slug": "4x4"}),
            ),
            backend_ref={"taxonomy": "pa_tile-size"},
        )
        self.category_by_key = {
            "wall-tiles": CatalogCategory(
                key="wall-tiles",
                name="Wall Tiles",
                parent_key=None,
                count=10,
                backend_ref={"id": 7, "parent_id": 0},
            ),
            "floor-tiles": CatalogCategory(
                key="floor-tiles",
                name="Floor Tiles",
                parent_key=None,
                count=8,
                backend_ref={"id": 8, "parent_id": 0},
            ),
        }

    def resolve_attribute(self, key):
        if key == "color":
            return self._color
        if key in ("tile-size", "tile size"):
            return self._size
        return None

    def resolve_attribute_term(self, attr_key, term_key_or_name):
        attr = self.resolve_attribute(attr_key)
        if not attr:
            return None
        needle = term_key_or_name.lower().strip()
        for term in attr.terms:
            if term.key.lower() == needle or term.name.lower() == needle:
                return term
        return None

    def resolve_category(self, key):
        return self.category_by_key.get(key.lower().strip())


def _body(call):
    return json.loads(json.dumps(call.body, sort_keys=True))


# ─── Representative query bodies (captured from Phase 4b.8 baseline) ───
#
# IMPORTANT: These exact JSON bodies are the contract. Any change to the Woo
# plugin request format MUST be reflected here and explained in the PR.
#

QUERY_1_EXPECTED = {
    "filters": {"conditions": [{"operator": "IN", "taxonomy": "pa_color", "terms": ["red"]}], "relation": "AND"},
    "page": 1,
    "per_page": 4,
}

QUERY_2_EXPECTED = {
    "filters": {"conditions": [{"operator": "IN", "taxonomy": "pa_color", "terms": ["red", "blue"]}], "relation": "AND"},
    "page": 1,
    "per_page": 4,
}

QUERY_3_EXPECTED = {
    "page": 1,
    "per_page": 4,
    "price": {"max": 100.0, "min": 10.0},
    "stock_status": "instock",
}

QUERY_4_EXPECTED = {
    "filters": {
        "conditions": [
            {"operator": "IN", "taxonomy": "product_cat", "terms": ["wall-tiles"]},
            {"operator": "IN", "taxonomy": "pa_color", "terms": ["blue"]},
        ],
        "relation": "AND",
    },
    "page": 1,
    "per_page": 4,
}

QUERY_5_EXPECTED = {
    "filters": {
        "conditions": [
            {"operator": "NOT IN", "taxonomy": "pa_color", "terms": ["red"]},
            {"operator": "IN", "taxonomy": "product_tag", "terms": ["quick-ship"]},
        ],
        "relation": "AND",
    },
    "page": 1,
    "per_page": 4,
    "stock_status": "outofstock",
}


@pytest.fixture
def fake_loader():
    return _FakeLoader()


def test_query1_single_attribute_filter(fake_loader):
    """Single attribute: color=red."""
    with patch("api_builder.filter_builder.loader", return_value=fake_loader):
        call = build_advanced_filter_call(attributes={"color": "red"})
    assert _body(call) == QUERY_1_EXPECTED


def test_query2_multi_value_attribute_filter(fake_loader):
    """Multi-value attribute: color=red,blue (single taxonomy → single IN condition)."""
    with patch("api_builder.filter_builder.loader", return_value=fake_loader):
        call = build_advanced_filter_call(attributes={"color": "red,blue"})
    assert _body(call) == QUERY_2_EXPECTED


def test_query3_price_range_and_stock_filter(fake_loader):
    """Price range + stock status (no taxonomy conditions)."""
    with patch("api_builder.filter_builder.loader", return_value=fake_loader):
        call = build_advanced_filter_call(
            min_price=10.0,
            max_price=100.0,
            in_stock=True,
        )
    assert _body(call) == QUERY_3_EXPECTED


def test_query4_category_and_attribute(fake_loader):
    """Category + attribute combination."""
    with patch("api_builder.filter_builder.loader", return_value=fake_loader):
        call = build_advanced_filter_call(
            categories=["wall-tiles"],
            attributes={"color": "blue"},
        )
    assert _body(call) == QUERY_4_EXPECTED


def test_query5_tag_excluded_attribute_and_stock(fake_loader):
    """Tag filter + excluded attribute + out-of-stock."""
    with patch("api_builder.filter_builder.loader", return_value=fake_loader):
        call = build_advanced_filter_call(
            tags=["quick-ship"],
            excluded_attributes={"color": ["red"]},
            in_stock=False,
        )
    assert _body(call) == QUERY_5_EXPECTED


def test_stock_and_price_not_in_filters_conditions(fake_loader):
    """stock_status and price must appear as top-level body keys, not inside body['filters']."""
    with patch("api_builder.filter_builder.loader", return_value=fake_loader):
        call = build_advanced_filter_call(
            attributes={"color": "red"},
            min_price=5.0,
            in_stock=True,
        )
    body = call.body
    # Top-level keys
    assert "stock_status" in body
    assert "price" in body
    # NOT nested inside filters
    if "filters" in body:
        for cond in body["filters"].get("conditions", []):
            assert cond.get("taxonomy") not in ("stock_status", "price")


def test_product_id_removes_stock_and_filters(fake_loader):
    """When product_id is set, stock_status and filters are stripped from body."""
    with patch("api_builder.filter_builder.loader", return_value=fake_loader):
        call = build_advanced_filter_call(
            attributes={"color": "red"},
            in_stock=True,
            product_id=42,
        )
    body = call.body
    assert "ids" in body
    assert body["ids"] == [42]
    assert "stock_status" not in body
    assert "filters" not in body
