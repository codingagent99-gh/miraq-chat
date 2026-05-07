import pytest

import classifier.extractors as extractors_module
from classifier.extractors import _resolve_attribute_or_tag, extract_attributes, extract_category, extract_tag
from models import ExtractedEntities
from store_loader.lookup_builder import build_all_lookups
from store_loader.queries import StoreQueryMixin
from store_registry import set_store_loader


class _FixtureLoader(StoreQueryMixin):
    def __init__(self):
        self.all_attributes_raw = [
            {
                "visible": True,
                "taxonomy": "pa_color",
                "attribute_id": 17,
                "attribute_name": "color",
                "attribute_label": "Color",
                "terms": [
                    {"id": 1, "slug": "red", "name": "Red", "count": 10},
                    {"id": 2, "slug": "blue", "name": "Blue", "count": 10},
                ],
            },
            {
                "visible": True,
                "taxonomy": "pa_finish",
                "attribute_id": 18,
                "attribute_name": "finish",
                "attribute_label": "Finish",
                "terms": [
                    {"id": 3, "slug": "matte", "name": "Matte", "count": 8},
                    {"id": 4, "slug": "glossy", "name": "Glossy", "count": 8},
                ],
            },
            {
                "visible": True,
                "taxonomy": "pa_tile-size",
                "attribute_id": 19,
                "attribute_name": "tile-size",
                "attribute_label": "Tile Size",
                "terms": [
                    {"id": 5, "slug": "12-x-12", "name": "12x12", "count": 7},
                    {"id": 6, "slug": "24-x-24", "name": "24x24", "count": 7},
                ],
            },
            {
                "visible": True,
                "taxonomy": "pa_origin",
                "attribute_id": 20,
                "attribute_name": "origin",
                "attribute_label": "Origin",
                "terms": [
                    {"id": 7, "slug": "italy", "name": "Italy", "count": 6},
                    {"id": 8, "slug": "spain", "name": "Spain", "count": 6},
                ],
            },
        ]
        self.categories = [
            {"id": 100, "name": "Wall Tiles", "slug": "wall-tiles", "count": 10, "parent": 0},
            {"id": 101, "name": "Floor Tiles", "slug": "floor-tiles", "count": 10, "parent": 0},
        ]
        self.tags = [
            {"id": 200, "name": "Quick Ship", "slug": "quick-ship", "count": 12},
            {"id": 201, "name": "Made in Italy", "slug": "made-in-italy", "count": 5},
        ]
        self.products = [{"id": 300, "name": "Aura Tile", "slug": "aura-tile"}]
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


@pytest.fixture()
def loader():
    l = _FixtureLoader()
    build_all_lookups(l)
    set_store_loader(l)
    try:
        yield l
    finally:
        set_store_loader(None)


def _run_extractors(query: str) -> ExtractedEntities:
    entities = ExtractedEntities()
    text = query.lower()
    extract_category(text, entities)
    extract_attributes(text, entities)
    extract_tag(text, entities)
    return entities


def _normalize_attr_key_for_loader(attr_key: str) -> str:
    return attr_key.replace(" ", "-")


@pytest.mark.parametrize(
    ("query", "expected_attrs"),
    [
        ("show red tiles", {"color": "red"}),
        ("show blue tiles", {"color": "blue"}),
        ("show matte tiles", {"finish": "matte"}),
        ("show glossy tiles", {"finish": "glossy"}),
        ("show 12x12 tiles", {"tile size": "12-x-12"}),
        ("show 24x24 tiles", {"tile size": "24-x-24"}),
        ("show italian tiles", {"origin": "italy"}),
        ("show spanish tiles", {"origin": "spain"}),
        ("show red wall tiles", {"color": "red"}),
        ("show glossy floor tiles", {"finish": "glossy"}),
    ],
)
def test_classifier_attribute_keys_remain_loader_resolvable_and_legacy_compatible(loader, query, expected_attrs):
    entities = _run_extractors(query)

    assert entities.attributes == expected_attrs
    for attr_key in entities.attributes:
        assert _normalize_attr_key_for_loader(attr_key) in loader.attribute_by_key


@pytest.mark.parametrize(
    "query",
    [
        "quick ship wall tiles",
        "made in italy floor tiles",
        "show red wall tiles",
        "show glossy floor tiles",
    ],
)
def test_classifier_tag_and_category_keys_match_neutral_indexes(loader, query):
    entities = _run_extractors(query)

    for tag_key in entities.tag_slugs:
        assert tag_key in loader.tag_by_key
    for category_key in entities.target_category_slugs:
        assert category_key in loader.category_by_key


def test_fallback_to_legacy_attribute_when_neutral_lookup_fails(loader, monkeypatch):
    entities = ExtractedEntities()
    term = {"id": 5, "slug": "12-x-12", "name": "12x12"}
    debug_calls = []
    monkeypatch.setattr(loader, "resolve_attribute", lambda _key: None)
    monkeypatch.setattr(extractors_module.logger, "debug", lambda *args, **kwargs: debug_calls.append(args))

    _resolve_attribute_or_tag(
        entities=entities,
        loader=loader,
        original_text="show 12x12 tiles",
        taxonomy="pa_tile-size",
        label="tile size",
        term=term,
        term_name_lower="12x12",
        is_dimensional=True,
        matched_pattern="12x12",
    )

    assert entities.attributes == {"tile size": "12-x-12"}
    assert any("resolve_attribute failed" in msg[0] for msg in debug_calls)
