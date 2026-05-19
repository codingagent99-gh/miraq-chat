from unittest.mock import patch

from api_builder.filter_builder import build_advanced_filter_call
from models.catalog import CatalogAttribute, CatalogAttributeTerm


class _FakeLoader:
    def __init__(self):
        self._color = CatalogAttribute(
            key="color",
            label="Color",
            terms=(
                CatalogAttributeTerm(key="red", name="Red", backend_ref={"slug": "red"}),
                CatalogAttributeTerm(key="blue", name="Blue", backend_ref={"slug": "blue"}),
                CatalogAttributeTerm(key="green", name="Green", backend_ref={"slug": "green"}),
            ),
            backend_ref={"taxonomy": "pa_color"},
        )

    def resolve_attribute(self, key):
        return self._color if key == "color" else None

    def resolve_attribute_term(self, attr_key, term_key_or_name):
        if attr_key != "color":
            return None
        needle = term_key_or_name.lower().strip()
        for term in self._color.terms:
            if term.key.lower() == needle or term.name.lower() == needle:
                return term
        return None


def _first_condition(call):
    return call.body["filters"]["conditions"][0]


def test_build_advanced_filter_call_uses_neutral_attribute_key():
    with patch("api_builder.filter_builder.loader", return_value=_FakeLoader()):
        call = build_advanced_filter_call(attributes={"color": "red"})
    assert _first_condition(call) == {
        "taxonomy": "pa_color",
        "terms": ["red"],
        "operator": "IN",
    }


def test_build_advanced_filter_call_supports_neutral_multi_value():
    with patch("api_builder.filter_builder.loader", return_value=_FakeLoader()):
        call = build_advanced_filter_call(attributes={"color": "red,blue"})
    assert _first_condition(call) == {
        "taxonomy": "pa_color",
        "terms": ["red", "blue"],
        "operator": "IN",
    }


def test_build_advanced_filter_call_maps_excluded_attributes_from_neutral_key():
    with patch("api_builder.filter_builder.loader", return_value=_FakeLoader()):
        call = build_advanced_filter_call(excluded_attributes={"color": ["green"]})
    assert _first_condition(call) == {
        "taxonomy": "pa_color",
        "terms": ["green"],
        "operator": "NOT IN",
    }


def test_build_advanced_filter_call_keeps_legacy_taxonomy_key_with_warning():
    with patch("api_builder.filter_builder.loader", return_value=_FakeLoader()):
        with patch("api_builder.filter_builder.logger.warning") as warning_mock:
            call = build_advanced_filter_call(attributes={"pa_color": "red"})

    warning_mock.assert_called_once()
    assert _first_condition(call) == {
        "taxonomy": "pa_color",
        "terms": ["red"],
        "operator": "IN",
    }


def test_neutral_and_legacy_filters_produce_identical_json_for_representative_queries():
    loader = _FakeLoader()
    with patch("api_builder.filter_builder.loader", return_value=loader):
        neutral_single = build_advanced_filter_call(attributes={"color": "red"}).body
        legacy_single = build_advanced_filter_call(attributes={"pa_color": "red"}).body

        neutral_multi = build_advanced_filter_call(attributes={"color": "red,blue"}).body
        legacy_multi = build_advanced_filter_call(attributes={"pa_color": "red,blue"}).body

        neutral_excluded = build_advanced_filter_call(excluded_attributes={"color": ["green"]}).body
        legacy_excluded = build_advanced_filter_call(excluded_attributes={"pa_color": ["green"]}).body

    assert neutral_single == legacy_single
    assert neutral_multi == legacy_multi
    assert neutral_excluded == legacy_excluded
