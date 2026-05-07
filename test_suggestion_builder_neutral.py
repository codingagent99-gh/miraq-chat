from handlers.suggestion_builder import build_suggestions
from models import ExtractedEntities
from store_loader.lookup_builder import build_all_lookups
from store_loader.queries import StoreQueryMixin


class MockStoreLoader(StoreQueryMixin):
    def __init__(self):
        self.all_attributes_raw = [
            {
                "visible": True,
                "taxonomy": "pa_color",
                "attribute_id": 17,
                "attribute_name": "color",
                "attribute_label": "Color",
                "terms": [
                    {"id": 42, "slug": "red", "name": "Red", "count": 12},
                    {"id": 43, "slug": "blue", "name": "Blue", "count": 9},
                    {"id": 44, "slug": "white", "name": "White", "count": 3},
                ],
            },
            {
                "visible": True,
                "taxonomy": "pa_tile-size",
                "attribute_id": 18,
                "attribute_name": "tile-size",
                "attribute_label": "Tile Size",
                "terms": [
                    {"id": 51, "slug": "2x2", "name": "2x2", "count": 5},
                    {"id": 52, "slug": "4x4", "name": "4x4", "count": 2},
                ],
            },
        ]
        self.categories = []
        self.tags = []
        self.products = []
        self.attribute_terms = {}
        self._category_synonyms = {}
        self._store_generic_terms = set()

        self.attribute_by_slug = {}
        self.attribute_by_id = {}
        self.category_by_slug = {}
        self.category_by_id = {}
        self.category_by_name_lower = {}
        self.category_slugs_by_name = {}
        self.tag_by_slug = {}
        self.tag_by_id = {}
        self.tag_by_name_lower = {}
        self.product_by_name_lower = {}
        self.product_name_tokens = []
        self.category_keywords = {}
        self.longest_match_catalog = []
        self.attribute_by_key = {}
        self.category_by_key = {}
        self.tag_by_key = {}


def _build_loader():
    loader = MockStoreLoader()
    build_all_lookups(loader)
    return loader


def test_build_suggestions_uses_neutral_attribute_lookup():
    loader = _build_loader()
    entities = ExtractedEntities(attributes={"color": "red"})

    suggestions = build_suggestions(entities, loader)

    assert len(suggestions) == 1
    assert suggestions[0]["type"] == "attribute"
    assert suggestions[0]["label"] == "Try Blue color"
    assert suggestions[0]["attributes"] == {"color": "Blue"}


def test_build_suggestions_unknown_attribute_returns_no_siblings():
    loader = _build_loader()
    entities = ExtractedEntities(attributes={"unknown-attr": "value"})

    suggestions = build_suggestions(entities, loader)

    assert suggestions == []
