from unittest.mock import patch

from api_builder.or_pairs import build_or_pair_conditions, resolve_or_pair
from models import OrPair
from models.catalog import CatalogAttribute, CatalogAttributeTerm


class _FakeLoader:
    def __init__(self):
        self._attr = CatalogAttribute(
            key="color",
            label="Color",
            terms=(
                CatalogAttributeTerm(key="red", name="Red", backend_ref={"slug": "red"}),
            ),
            backend_ref={"taxonomy": "pa_color"},
        )

    def resolve_attribute(self, key):
        return self._attr if key == "color" else None

    def resolve_attribute_term(self, attr_key, term_key_or_name):
        if attr_key != "color":
            return None
        if term_key_or_name.lower().strip() == "red":
            return self._attr.terms[0]
        return None


def test_resolve_or_pair_uses_neutral_attr_key():
    pair = OrPair(attr_key="color", attr_term="red")
    with patch("api_builder.or_pairs.loader", return_value=_FakeLoader()):
        resolved = resolve_or_pair(pair)
        assert resolved.attr_taxonomy == "pa_color"
        assert resolved.attr_term == "red"

        conditions, _, _ = build_or_pair_conditions([pair])
        assert conditions == [
            {
                "taxonomy": "pa_color",
                "field": "slug",
                "terms": ["red"],
                "operator": "IN",
            }
        ]


def test_resolve_or_pair_deprecated_attr_taxonomy_alias_logs_warning():
    with patch("models.domain.logger.warning") as warning_mock:
        pair = OrPair(attr_taxonomy="pa_color", attr_term="red")
    warning_mock.assert_called_once_with("OrPair.attr_taxonomy is deprecated; use attr_key")

    with patch("api_builder.or_pairs.loader", return_value=_FakeLoader()):
        resolved = resolve_or_pair(pair)
        assert resolved.attr_key == "color"
        assert resolved.attr_taxonomy == "pa_color"
        assert resolved.attr_term == "red"

        conditions, _, _ = build_or_pair_conditions([pair])
        assert conditions == [
            {
                "taxonomy": "pa_color",
                "field": "slug",
                "terms": ["red"],
                "operator": "IN",
            }
        ]


def test_or_pair_branches_and_is_valid_still_work():
    assert OrPair(tag_slug="quick-ship", attr_key="color", attr_term="red").branches == 2
    assert OrPair(tag_slug="quick-ship", attr_key="color", attr_term="red").is_valid is True
    assert OrPair(attr_key="color", attr_term="red").branches == 1
    assert OrPair(attr_key="color", attr_term="red").is_valid is False
