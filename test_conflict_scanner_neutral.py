from types import SimpleNamespace

import conflict_scanner
from models.catalog import CatalogAttribute, CatalogAttributeTerm


class _LoaderStub:
    def __init__(self):
        color = CatalogAttribute(
            key="color",
            label="Color",
            terms=(CatalogAttributeTerm(key="red", name="Red"),),
        )
        self._attrs = {"color": color}

    def resolve_attribute(self, key):
        return self._attrs.get(str(key).lower())

    def resolve_attribute_term(self, attr_key, term_key_or_name):
        attr = self.resolve_attribute(attr_key)
        if not attr:
            return None
        needle = str(term_key_or_name).lower()
        for term in attr.terms:
            if term.key.lower() == needle or term.name.lower() == needle:
                return term
        return None


def _mock_classify_result(attributes):
    entities = SimpleNamespace(
        product_name=None,
        target_category_slugs=set(),
        category_name=None,
        attributes=attributes,
        tag_slugs=[],
        attr_tag_or_pairs=[],
    )
    return SimpleNamespace(
        entities=entities,
        intent=SimpleNamespace(value="filter_by_attribute"),
        confidence=0.99,
    )


def test_simulate_single_term_uses_loader_display_values_for_attributes(monkeypatch):
    monkeypatch.setattr(conflict_scanner, "classify", lambda _term: _mock_classify_result({"color": "red"}))
    monkeypatch.setattr(conflict_scanner, "build_api_calls", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(conflict_scanner, "get_store_loader", lambda: _LoaderStub())

    result = conflict_scanner.simulate_single_term("red")

    assert "Attr (Color) [Red]" in result["locations"]


def test_simulate_single_term_falls_back_for_unknown_attribute_key(monkeypatch):
    monkeypatch.setattr(conflict_scanner, "classify", lambda _term: _mock_classify_result({"mystery-attr": "mystery-term"}))
    monkeypatch.setattr(conflict_scanner, "build_api_calls", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(conflict_scanner, "get_store_loader", lambda: _LoaderStub())

    result = conflict_scanner.simulate_single_term("mystery-term")

    assert "Attr (Mystery-Attr) [mystery-term]" in result["locations"]


def test_serialize_entities_accepts_attr_key_and_legacy_attr_taxonomy():
    entities = SimpleNamespace(
        attr_tag_or_pairs=[
            {"tag_slug": "quick-ship", "attr_key": "color", "attr_term": "red"},
            {"tag_slug": "chip-card", "attr_taxonomy": "pa_finish", "attr_term": "matte"},
        ]
    )

    serialized = conflict_scanner._serialize_entities(entities)
    pairs = serialized["attr_tag_or_pairs"]

    assert pairs[0]["attr_key"] == "color"
    assert pairs[1]["attr_key"] == "finish"
