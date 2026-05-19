import pytest

from models import ExtractedEntities
from models.catalog import CatalogAttribute, CatalogAttributeTerm
from formatters import format_variation
from response_generator import _build_search_context_string
from store_registry import set_store_loader


class _FixtureLoader:
    def __init__(self):
        self.attribute_by_key = {
            "color": CatalogAttribute(
                key="color",
                label="Color",
                terms=(CatalogAttributeTerm(key="red", name="Red"),),
            )
        }

    def resolve_attribute(self, key):
        return self.attribute_by_key.get(str(key).lower().strip())

    def resolve_attribute_term(self, attr_key, term_key_or_name):
        attr = self.resolve_attribute(attr_key)
        if not attr:
            return None
        needle = str(term_key_or_name).lower().strip()
        for term in attr.terms:
            if term.key == needle or term.name.lower() == needle:
                return term
        return None

    def resolve_tag(self, key):
        return None

    def resolve_category(self, key):
        return None


@pytest.fixture
def fixture_loader():
    loader = _FixtureLoader()
    set_store_loader(loader)
    yield loader
    set_store_loader(None)


def test_search_context_uses_resolved_attribute_label_and_term_name(fixture_loader):
    entities = ExtractedEntities(attributes={"color": "red"})
    result = _build_search_context_string(entities)

    assert "Color: **Red**" in result


def test_search_context_falls_back_for_unresolvable_attribute_key(fixture_loader):
    entities = ExtractedEntities(attributes={"tile-size": "extra-large"})
    result = _build_search_context_string(entities)

    assert "Tile Size: **Extra Large**" in result


def test_format_variation_uses_resolved_attribute_label_and_term_name(fixture_loader):
    raw_variation = {"id": 101, "attributes": {"pa_color": "red"}, "stock_status": "instock"}
    formatted = format_variation(raw_variation, parent={"id": 1, "name": "Sample"})

    assert formatted["attributes"] == [{"name": "Color", "option": "Red"}]
