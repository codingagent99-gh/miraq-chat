import pytest

from handlers.chat_utils import build_variant_prompt
from handlers.variant_handler import _build_display_to_slug, _variation_matches_resolved
from models.catalog import CatalogAttribute, CatalogAttributeTerm
from store_registry import set_store_loader


class _FixtureLoader:
    def __init__(self, taxonomy_overrides=None):
        taxonomy_overrides = taxonomy_overrides or {}
        color_taxonomy = taxonomy_overrides.get("color", "pa_color")
        size_taxonomy = taxonomy_overrides.get("size", "pa_size")
        finish_taxonomy = taxonomy_overrides.get("finish", "pa_finish")
        self.attribute_by_key = {
            "color": CatalogAttribute(
                key="color",
                label="Color",
                terms=(
                    CatalogAttributeTerm(key="red", name="red", backend_ref={"slug": "red"}),
                    CatalogAttributeTerm(key="blue", name="blue", backend_ref={"slug": "blue"}),
                ),
                backend_ref={"taxonomy": color_taxonomy} if color_taxonomy is not None else {},
            ),
            "size": CatalogAttribute(
                key="size",
                label="Size",
                terms=(
                    CatalogAttributeTerm(key="small", name="small", backend_ref={"slug": "small"}),
                    CatalogAttributeTerm(key="large", name="large", backend_ref={"slug": "large"}),
                    CatalogAttributeTerm(key="12-x-12", name="12 x 12", backend_ref={"slug": "12-x-12"}),
                    CatalogAttributeTerm(key="24-x-24", name="24 x 24", backend_ref={"slug": "24-x-24"}),
                ),
                backend_ref={"taxonomy": size_taxonomy} if size_taxonomy is not None else {},
            ),
            "finish": CatalogAttribute(
                key="finish",
                label="Finish",
                terms=(
                    CatalogAttributeTerm(key="matte", name="matte", backend_ref={"slug": "matte"}),
                    CatalogAttributeTerm(key="gloss", name="gloss", backend_ref={"slug": "gloss"}),
                ),
                backend_ref={"taxonomy": finish_taxonomy} if finish_taxonomy is not None else {},
            ),
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


@pytest.fixture(autouse=True)
def _reset_store_loader():
    set_store_loader(None)
    yield
    set_store_loader(None)


def test_variation_matches_resolved_uses_neutral_attribute_lookup():
    loader = _FixtureLoader()
    set_store_loader(loader)
    display_to_slug = _build_display_to_slug(loader)
    variations = [
        {"id": 101, "attributes": [{"name": "Color", "option": "red"}, {"name": "Size", "option": "small"}]},
        {"id": 102, "attributes": [{"name": "Color", "option": "red"}, {"name": "Size", "option": "large"}]},
        {"id": 103, "attributes": [{"name": "Color", "option": "blue"}, {"name": "Size", "option": "small"}]},
    ]

    matched_ids = [
        variation["id"]
        for variation in variations
        if _variation_matches_resolved(variation, {"color": "red"}, display_to_slug)
    ]

    assert matched_ids == [101, 102]


def test_build_display_to_slug_supports_woo_and_non_pa_taxonomies():
    woo_loader = _FixtureLoader()
    shopify_loader = _FixtureLoader(taxonomy_overrides={"color": None})

    woo_result = _build_display_to_slug(woo_loader)
    shopify_result = _build_display_to_slug(shopify_loader)

    assert woo_result["pa_color"]["red"] == "red"
    assert shopify_result["color"]["red"] == "red"


def test_build_variant_prompt_prompt_text_stays_stable():
    loader = _FixtureLoader()
    set_store_loader(loader)
    display_to_slug = _build_display_to_slug(loader)
    parent_raw = {
        "attributes": [
            {"name": "pa_color", "variation": True, "options": ["red", "blue"]},
            {"name": "pa_size", "variation": True, "options": ["small", "large"]},
        ]
    }
    variations = [
        {"id": 201, "attributes": [{"name": "Color", "option": "red"}, {"name": "Size", "option": "small"}]},
        {"id": 202, "attributes": [{"name": "Color", "option": "red"}, {"name": "Size", "option": "large"}]},
        {"id": 203, "attributes": [{"name": "Color", "option": "blue"}, {"name": "Size", "option": "small"}]},
    ]

    prompt = build_variant_prompt(parent_raw, "Sample Tile", {"Color": "Red"}, variations, display_to_slug)

    assert prompt == (
        "I'd love to order **Sample Tile** for you! "
        "To make sure I get the right one, please choose from the following options:\n\n"
        "• **Size:** Large, Small\n"
    )


def test_build_variant_prompt_snapshot_for_realistic_five_variation_product():
    loader = _FixtureLoader()
    set_store_loader(loader)
    display_to_slug = _build_display_to_slug(loader)
    parent_raw = {
        "attributes": [
            {"name": "pa_color", "variation": True, "options": []},
            {"name": "pa_size", "variation": True, "options": []},
            {"name": "pa_finish", "variation": True, "options": []},
        ]
    }
    variations = [
        {"id": 301, "attributes": [{"name": "Color", "option": "red"}, {"name": "Size", "option": "12-x-12"}, {"name": "Finish", "option": "matte"}]},
        {"id": 302, "attributes": [{"name": "Color", "option": "red"}, {"name": "Size", "option": "24-x-24"}, {"name": "Finish", "option": "matte"}]},
        {"id": 303, "attributes": {"pa_color": "red", "pa_size": "12-x-12", "pa_finish": "gloss"}},
        {"id": 304, "attributes": {"pa_color": "blue", "pa_size": "12-x-12", "pa_finish": "matte"}},
        {"id": 305, "attributes": {"pa_color": "blue", "pa_size": "24-x-24", "pa_finish": "gloss"}},
    ]

    prompt = build_variant_prompt(parent_raw, "Aura Tile", {"Color": "Red"}, variations, display_to_slug)

    assert prompt == (
        "I'd love to order **Aura Tile** for you! "
        "To make sure I get the right one, please choose from the following options:\n\n"
        "• **Size:** 12 X 12, 24 X 24\n"
        "• **Finish:** Gloss, Matte\n"
    )
