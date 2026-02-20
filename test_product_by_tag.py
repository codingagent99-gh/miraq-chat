"""
Tests for PRODUCT_BY_TAG intent:
- _extract_tag() generic entity extractor
- PRODUCT_BY_TAG intent classification
- api_builder PRODUCT_BY_TAG handler
- Existing domain-specific tag behaviour not broken
"""

import json
import pytest
from classifier import classify, _extract_tag
from models import Intent, ClassifiedResult, ExtractedEntities
from api_builder import build_api_calls, CUSTOM_API_BASE, BASE
from store_loader import StoreLoader
from store_registry import set_store_loader, get_store_loader


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_loader_with_tags(tags):
    """Return a minimal StoreLoader pre-loaded with given tags (no network)."""
    loader = StoreLoader()
    # Inject tag data without hitting the network
    loader.tags = tags
    loader.categories = []
    loader.attributes = []
    loader.attribute_terms = {}
    loader.products = []
    loader.all_attributes_raw = []

    # Manually invoke lookup building for tags only
    loader.tag_by_slug = {}
    loader.tag_by_id = {}
    loader.tag_by_name_lower = {}
    loader.category_by_slug = {}
    loader.category_by_id = {}
    loader.category_by_name_lower = {}
    loader.category_keywords = {}
    loader.attribute_by_slug = {}
    loader.attribute_by_id = {}
    loader.product_by_name_lower = {}
    loader.product_name_tokens = []

    for tag in tags:
        tag_id = tag["id"]
        slug = tag.get("slug", "")
        name_lower = tag.get("name", "").lower()
        entry = {
            "id": tag_id,
            "name": tag.get("name", ""),
            "slug": slug,
            "count": tag.get("count", 1),
        }
        loader.tag_by_slug[slug] = entry
        loader.tag_by_id[tag_id] = entry
        loader.tag_by_name_lower[name_lower] = entry
        slug_words = slug.replace("-", " ")
        if slug_words != name_lower:
            loader.tag_by_name_lower.setdefault(slug_words, entry)

    # Mark as loaded (needed for is_ready())
    import time
    loader._last_loaded = time.time()
    return loader


SAMPLE_TAGS = [
    {"id": 1221, "name": '1/2" Thick', "slug": "1-2-thick", "count": 5},
    {"id": 2527, "name": "10mm Thick", "slug": "10mm-thick", "count": 3},
    {"id": 1157, "name": "2022 Collection", "slug": "2022-collection", "count": 8},
    {"id": 58,   "name": "2025 collection", "slug": "2025-collection", "count": 12},
    {"id": 100,  "name": "Quick Ship", "slug": "quick-ship", "count": 20},
    {"id": 200,  "name": "Chip Card", "slug": "chip-card", "count": 15},
    # Generic tags not covered by any domain-specific extractor
    {"id": 300,  "name": "Heritage Series", "slug": "heritage-series", "count": 7},
    {"id": 400,  "name": "Featured Items", "slug": "featured-items", "count": 10},
]


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _extract_tag()
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractTag:

    @pytest.fixture(autouse=True)
    def setup_mock_loader(self):
        loader = _make_loader_with_tags(SAMPLE_TAGS)
        set_store_loader(loader)
        yield
        set_store_loader(None)

    def test_extracts_tag_by_name(self):
        entities = ExtractedEntities()
        _extract_tag("show me heritage series tiles", entities)
        assert 300 in entities.tag_ids
        assert "heritage-series" in entities.tag_slugs

    def test_extracts_tag_by_slug_words(self):
        entities = ExtractedEntities()
        _extract_tag("show me featured items", entities)
        assert 400 in entities.tag_ids
        assert "featured-items" in entities.tag_slugs

    def test_does_not_duplicate_existing_tag_ids(self):
        entities = ExtractedEntities()
        entities.tag_ids = [400]
        entities.tag_slugs = ["featured-items"]
        _extract_tag("show me featured items", entities)
        # Should not add a duplicate
        assert entities.tag_ids.count(400) == 1

    def test_skips_tags_with_count_zero(self):
        loader = _make_loader_with_tags([
            {"id": 999, "name": "Empty Tag", "slug": "empty-tag", "count": 0},
        ])
        set_store_loader(loader)
        entities = ExtractedEntities()
        _extract_tag("show me empty tag products", entities)
        assert 999 not in entities.tag_ids

    def test_skips_very_short_tag_names(self):
        loader = _make_loader_with_tags([
            {"id": 998, "name": "Big", "slug": "big", "count": 5},
        ])
        set_store_loader(loader)
        entities = ExtractedEntities()
        _extract_tag("show me big tiles", entities)
        assert 998 not in entities.tag_ids

    def test_no_loader_does_not_crash(self):
        set_store_loader(None)
        entities = ExtractedEntities()
        _extract_tag("show me spanish style tiles", entities)
        assert entities.tag_ids == []

    def test_extracts_multiple_tags(self):
        entities = ExtractedEntities()
        _extract_tag("show me heritage series and featured items", entities)
        assert 300 in entities.tag_ids
        assert 400 in entities.tag_ids


# ─────────────────────────────────────────────────────────────────────────────
# Tests for PRODUCT_BY_TAG intent classification
# ─────────────────────────────────────────────────────────────────────────────

class TestProductByTagClassification:

    @pytest.fixture(autouse=True)
    def setup_mock_loader(self):
        loader = _make_loader_with_tags(SAMPLE_TAGS)
        set_store_loader(loader)
        yield
        set_store_loader(None)

    def test_classifies_product_by_tag_for_generic_tag_query(self):
        result = classify("show me heritage series tiles")
        assert result.intent == Intent.PRODUCT_BY_TAG
        assert result.confidence >= 0.85
        assert 300 in result.entities.tag_ids
        assert "heritage-series" in result.entities.tag_slugs

    def test_classifies_product_by_tag_for_featured_items(self):
        result = classify("show me featured items")
        assert result.intent == Intent.PRODUCT_BY_TAG
        assert 400 in result.entities.tag_ids

    def test_chip_card_keyword_overrides_tag(self):
        """Chip card keyword rule (section 3) should still fire for 'chip card'."""
        result = classify("show me chip card tiles")
        assert result.intent == Intent.CHIP_CARD

    def test_quick_ship_keyword_overrides_tag(self):
        """Quick ship keyword rule (section 6) should still fire."""
        result = classify("show me quick ship tiles")
        assert result.intent == Intent.PRODUCT_QUICK_SHIP

    def test_collection_year_overrides_generic_tag(self):
        """Collection-year extractor sets collection_year; PRODUCT_BY_COLLECTION fires first."""
        result = classify("show me 2022 collection tiles")
        assert result.intent == Intent.PRODUCT_BY_COLLECTION

    def test_no_tag_match_does_not_classify_product_by_tag(self):
        """Utterance with no matching tag should not produce PRODUCT_BY_TAG."""
        result = classify("show me all tiles")
        assert result.intent != Intent.PRODUCT_BY_TAG


# ─────────────────────────────────────────────────────────────────────────────
# Tests for api_builder PRODUCT_BY_TAG handler
# ─────────────────────────────────────────────────────────────────────────────

class TestApiBuilderProductByTag:

    def _result(self, tag_slugs=None, tag_ids=None):
        entities = ExtractedEntities()
        entities.tag_slugs = tag_slugs or []
        entities.tag_ids = tag_ids or []
        return ClassifiedResult(
            intent=Intent.PRODUCT_BY_TAG,
            entities=entities,
            confidence=0.88,
        )

    def test_builds_advanced_filter_call_when_slugs_present(self):
        result = self._result(tag_slugs=["heritage-series"], tag_ids=[300])
        calls = build_api_calls(result)
        assert len(calls) == 1
        call = calls[0]
        assert call.is_custom_api
        assert "products-advanced" in call.endpoint
        filters = json.loads(call.params["filters"])
        tag_filters = [f for f in filters if "tag" in f]
        assert len(tag_filters) == 1
        assert "heritage-series" in tag_filters[0]["tag"]

    def test_falls_back_to_standard_api_when_no_slugs(self):
        result = self._result(tag_slugs=[], tag_ids=[300])
        calls = build_api_calls(result)
        assert len(calls) == 1
        call = calls[0]
        assert not call.is_custom_api
        assert call.endpoint.endswith("/products")
        assert call.params.get("tag") == "300"

    def test_description_includes_slug_info(self):
        result = self._result(tag_slugs=["heritage-series"], tag_ids=[300])
        calls = build_api_calls(result)
        assert "heritage-series" in calls[0].description

    def test_page_param_is_passed_through(self):
        result = self._result(tag_slugs=["featured-items"], tag_ids=[400])
        calls = build_api_calls(result, page=3)
        assert calls[0].params.get("page") == 3

    def test_multiple_slugs_included_in_filter(self):
        result = self._result(tag_slugs=["heritage-series", "featured-items"], tag_ids=[300, 400])
        calls = build_api_calls(result)
        assert len(calls) == 1
        filters = json.loads(calls[0].params["filters"])
        tag_filters = [f for f in filters if "tag" in f]
        assert len(tag_filters) == 1
        tag_value = tag_filters[0]["tag"]
        assert "heritage-series" in tag_value
        assert "featured-items" in tag_value

    def test_fallback_uses_first_tag_id_when_no_slugs(self):
        result = self._result(tag_slugs=[], tag_ids=[300, 400])
        calls = build_api_calls(result)
        assert calls[0].params.get("tag") == "300"


# ─────────────────────────────────────────────────────────────────────────────
# Tests that existing domain-specific tag behaviour is not broken
# ─────────────────────────────────────────────────────────────────────────────

class TestExistingTagBehaviourNotBroken:
    """Verify that adding _extract_tag() doesn't break the existing domain extractors."""

    @pytest.fixture(autouse=True)
    def setup_mock_loader(self):
        loader = _make_loader_with_tags(SAMPLE_TAGS)
        set_store_loader(loader)
        yield
        set_store_loader(None)

    def test_filter_by_finish_not_broken(self):
        result = classify("show me matte tiles")
        # finish extractor fires → FILTER_BY_FINISH should take priority
        assert result.intent == Intent.FILTER_BY_FINISH
        assert result.entities.finish is not None

    def test_product_quick_ship_not_broken(self):
        result = classify("show me quick ship tiles")
        assert result.intent == Intent.PRODUCT_QUICK_SHIP

    def test_chip_card_not_broken(self):
        result = classify("show me chip card")
        assert result.intent == Intent.CHIP_CARD

    def test_collection_year_not_broken(self):
        result = classify("show me 2025 collection")
        assert result.intent == Intent.PRODUCT_BY_COLLECTION
        assert result.entities.collection_year == "2025"

    def test_product_by_collection_api_not_broken(self):
        """Verify PRODUCT_BY_COLLECTION still builds correct API calls."""
        entities = ExtractedEntities()
        entities.collection_year = "2025"
        entities.tag_slugs = ["2025-collection"]
        entities.tag_ids = [58]
        cr = ClassifiedResult(
            intent=Intent.PRODUCT_BY_COLLECTION,
            entities=entities,
            confidence=0.89,
        )
        calls = build_api_calls(cr)
        assert len(calls) == 1
        filters = json.loads(calls[0].params["filters"])
        tag_filters = [f for f in filters if "tag" in f]
        assert len(tag_filters) == 1
        assert "2025-collection" in tag_filters[0]["tag"]
