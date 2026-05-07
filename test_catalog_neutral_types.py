from dataclasses import FrozenInstanceError

import pytest

from models.catalog import (
    CatalogAttribute,
    CatalogAttributeTerm,
    CatalogCategory,
    CatalogTag,
)
from store_loader.lookup_builder import build_all_lookups
from store_loader.queries import StoreQueryMixin


class _FixtureLoader(StoreQueryMixin):
    def __init__(self, *, attributes, categories, tags, products):
        self.all_attributes_raw = attributes
        self.categories = categories
        self.tags = tags
        self.products = products
        self._category_synonyms = {}
        self._store_generic_terms = set()

        self.attribute_by_id = {}
        self.category_by_id = {}
        self.category_by_name_lower = {}
        self.category_slugs_by_name = {}
        self.tag_by_id = {}
        self.tag_by_name_lower = {}
        self.product_by_name_lower = {}
        self.product_name_tokens = []
        self.category_keywords = {}
        self.longest_match_catalog = []
        self.attribute_by_key = {}
        self.category_by_key = {}
        self.tag_by_key = {}


def _build_loader(*, attributes, categories, tags, products=None):
    loader = _FixtureLoader(
        attributes=attributes,
        categories=categories,
        tags=tags,
        products=products or [{"id": 901, "name": "Sample Product", "slug": "sample-product"}],
    )
    build_all_lookups(loader)
    return loader


def test_catalog_types_are_frozen():
    term = CatalogAttributeTerm(key="red", name="Red")
    attr = CatalogAttribute(key="color", label="Color", terms=(term,))
    cat = CatalogCategory(key="wall-tiles", name="Wall Tiles")
    tag = CatalogTag(key="quick-ship", name="Quick Ship")

    with pytest.raises(FrozenInstanceError):
        attr.label = "Shade"
    with pytest.raises(FrozenInstanceError):
        term.name = "Crimson"
    with pytest.raises(FrozenInstanceError):
        cat.name = "Floor Tiles"
    with pytest.raises(FrozenInstanceError):
        tag.name = "Sale"


def test_backend_ref_is_mutable_by_design():
    term = CatalogAttributeTerm(key="red", name="Red", backend_ref={"slug": "red"})
    term.backend_ref["id"] = 42
    assert term.backend_ref["id"] == 42


def test_neutral_indexes_and_queries_dual_populated():
    loader = _build_loader(
        attributes=[
            {
                "visible": True,
                "taxonomy": "pa_color",
                "attribute_id": 17,
                "attribute_name": "color",
                "attribute_label": "Color",
                "terms": [
                    {"id": 42, "slug": "red", "name": "Red", "count": 11},
                    {"id": 43, "slug": "blue", "name": "Blue", "count": 7},
                ],
            }
        ],
        categories=[
            {"id": 1, "name": "Tiles", "slug": "tiles", "count": 18, "parent": 0},
            {"id": 7, "name": "Wall Tiles", "slug": "wall-tiles", "count": 9, "parent": 1},
        ],
        tags=[
            {"id": 501, "name": "Quick Ship", "slug": "quick-ship", "count": 12},
            {"id": 502, "name": "Chip Card", "slug": "chip-card", "count": 2},
        ],
    )

    assert "color" in loader.attribute_by_key
    color = loader.attribute_by_key["color"]
    assert color.label == "Color"
    assert color.backend_ref["taxonomy"] == "pa_color"
    assert color.backend_ref["id"] == loader.attribute_by_id[17]["id"]
    assert [t.key for t in color.terms] == ["red", "blue"]

    assert loader.category_by_key["wall-tiles"].backend_ref["id"] == loader.category_by_id[7]["id"]
    assert loader.tag_by_key["quick-ship"].backend_ref["id"] == loader.tag_by_id[501]["id"]

    assert loader.resolve_attribute("color") == color
    assert loader.resolve_attribute_term("color", "red").name == "Red"
    assert loader.resolve_attribute_term("color", "Red").key == "red"
    assert loader.resolve_category("wall-tiles").name == "Wall Tiles"
    assert loader.resolve_tag("quick-ship").name == "Quick Ship"

    assert len(loader.attribute_by_key) == 1
    assert len(loader.category_by_key) == 2
    assert len(loader.tag_by_key) == 2


def test_attribute_without_pa_prefix_uses_attribute_name_fallback():
    loader = _build_loader(
        attributes=[
            {
                "taxonomy": "finish",
                "attribute_id": 23,
                "attribute_name": "finish",
                "attribute_label": "Finish",
                "terms": [{"id": 99, "slug": "matte", "name": "Matte"}],
            }
        ],
        categories=[{"id": 1, "name": "Tiles", "slug": "tiles", "count": 5, "parent": 0}],
        tags=[{"id": 2, "name": "Quick Ship", "slug": "quick-ship", "count": 1}],
    )
    assert "finish" in loader.attribute_by_key
    assert loader.attribute_by_key["finish"].label == "Finish"


def test_attribute_with_empty_taxonomy_and_attribute_name_is_skipped():
    loader = _build_loader(
        attributes=[
            {
                "taxonomy": "",
                "attribute_id": 55,
                "attribute_name": "",
                "attribute_label": "Invalid",
                "terms": [{"id": 1, "slug": "x", "name": "X"}],
            }
        ],
        categories=[{"id": 1, "name": "Tiles", "slug": "tiles", "count": 5, "parent": 0}],
        tags=[{"id": 2, "name": "Quick Ship", "slug": "quick-ship", "count": 1}],
    )
    assert loader.attribute_by_key == {}


def test_category_with_empty_slug_is_skipped_from_neutral_index():
    loader = _build_loader(
        attributes=[
            {
                "taxonomy": "pa_color",
                "attribute_id": 17,
                "attribute_name": "color",
                "terms": [{"id": 42, "slug": "red", "name": "Red"}],
            }
        ],
        categories=[
            {"id": 1, "name": "Tiles", "slug": "tiles", "count": 5, "parent": 0},
            {"id": 2, "name": "Bad Cat", "slug": "", "count": 3, "parent": 0},
        ],
        tags=[{"id": 2, "name": "Quick Ship", "slug": "quick-ship", "count": 1}],
    )
    assert "tiles" in loader.category_by_key
    assert "" not in loader.category_by_key
