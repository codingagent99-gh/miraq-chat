import json
from unittest.mock import patch

from ecommerce.woo_endpoints import WooEndpoints
from models.catalog import CatalogAttribute, CatalogAttributeTerm


class _FakeLoader:
    def __init__(self):
        self._attrs = [
            CatalogAttribute(
                key="color",
                label="Color",
                terms=(
                    CatalogAttributeTerm(key="red", name="Red", backend_ref={"slug": "red"}),
                    CatalogAttributeTerm(key="blue", name="Blue", backend_ref={"slug": "blue"}),
                ),
                backend_ref={"taxonomy": "pa_color"},
            ),
            CatalogAttribute(
                key="size",
                label="Size",
                terms=(
                    CatalogAttributeTerm(key="small", name="Small", backend_ref={"slug": "small"}),
                    CatalogAttributeTerm(key="large", name="Large", backend_ref={"slug": "large"}),
                ),
                backend_ref={"taxonomy": "pa_size"},
            ),
            CatalogAttribute(
                key="finish",
                label="Finish",
                terms=(
                    CatalogAttributeTerm(key="matte", name="Matte", backend_ref={"slug": "matte"}),
                    CatalogAttributeTerm(key="gloss", name="Gloss", backend_ref={"slug": "gloss"}),
                ),
                backend_ref={"taxonomy": "pa_finish"},
            ),
        ]
        self._by_key = {a.key: a for a in self._attrs}
        self._by_label = {a.label.lower(): a for a in self._attrs}

    def resolve_attribute(self, key):
        needle = str(key).lower().strip()
        return self._by_key.get(needle) or self._by_label.get(needle)

    def resolve_attribute_term(self, attr_key, term_key_or_name):
        attr = self.resolve_attribute(attr_key)
        if not attr:
            return None
        needle = str(term_key_or_name).lower().strip()
        for term in attr.terms:
            if term.key.lower() == needle or term.name.lower() == needle:
                return term
        return None


def _snapshot(payload):
    return json.dumps(payload, separators=(",", ":"))


def test_cart_payload_variant_id_all_axes_fixed_snapshot():
    ep = WooEndpoints()
    loader = _FakeLoader()
    resolved = {"Color": "Blue", "Size": "Large"}
    var_data = {"attributes": [{"name": "Color", "option": "blue"}, {"name": "Size", "option": "large"}]}
    expected = '[{"attribute":"pa_color","value":"blue"},{"attribute":"pa_size","value":"large"}]'

    with patch("ecommerce.woo_endpoints.woo_client.execute", return_value={"success": True, "data": var_data}):
        got = ep.build_cart_variation_payload(
            product_id=10,
            variant_id=99,
            resolved_attrs=resolved,
            store_loader=loader,
        )

    assert _snapshot(got) == expected


def test_cart_payload_variant_id_with_wildcard_axes_snapshot():
    ep = WooEndpoints()
    loader = _FakeLoader()
    resolved = {"Color": "Blue", "Finish": "Matte"}
    var_data = {"attributes": [{"name": "Color", "option": "blue"}]}
    expected = '[{"attribute":"pa_color","value":"blue"},{"attribute":"pa_finish","value":"matte"}]'

    with patch("ecommerce.woo_endpoints.woo_client.execute", return_value={"success": True, "data": var_data}):
        got = ep.build_cart_variation_payload(
            product_id=10,
            variant_id=99,
            resolved_attrs=resolved,
            store_loader=loader,
        )

    assert _snapshot(got) == expected


def test_cart_payload_no_variant_id_snapshot():
    ep = WooEndpoints()
    loader = _FakeLoader()
    resolved = {"Color": "Blue", "Finish": "Matte"}
    expected = '[{"attribute":"pa_color","value":"blue"},{"attribute":"pa_finish","value":"matte"}]'

    got = ep.build_cart_variation_payload(
        product_id=10,
        variant_id=None,
        resolved_attrs=resolved,
        store_loader=loader,
    )

    assert _snapshot(got) == expected


def test_cart_payload_variant_fetch_failure_falls_back_snapshot():
    ep = WooEndpoints()
    loader = _FakeLoader()
    resolved = {"Color": "Blue", "Unknown Label": "Value 123"}
    expected = '[{"attribute":"pa_color","value":"blue"},{"attribute":"pa_unknown-label","value":"value123"}]'

    with patch("ecommerce.woo_endpoints.woo_client.execute", return_value={"success": False, "data": None}):
        got = ep.build_cart_variation_payload(
            product_id=10,
            variant_id=99,
            resolved_attrs=resolved,
            store_loader=loader,
        )

    assert _snapshot(got) == expected
