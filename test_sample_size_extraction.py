"""
Tests for Sample Size Extraction Feature

Validates that _extract_sample_size() correctly extracts sample sizes
and that _extract_size() skips when sample_size is already set.
"""

import pytest
from classifier import classify
from models import Intent


class TestSampleSizeExtraction:
    """Test sample size extraction functionality."""

    def test_sample_size_numeric_pattern_12x24(self):
        """Test extraction of numeric sample size '12x24'."""
        result = classify("show me tiles of sample size 12x24")
        
        # Should extract sample_size
        assert result.entities.sample_size is not None, (
            "Expected sample_size to be extracted from 'sample size 12x24'"
        )
        # Should be formatted with quotes
        assert '12' in result.entities.sample_size and '24' in result.entities.sample_size, (
            f"Expected sample_size to contain '12' and '24' but got '{result.entities.sample_size}'"
        )
        # Should set attribute_slug to pa_sample-size
        assert result.entities.attribute_slug == "pa_sample-size", (
            f"Expected attribute_slug='pa_sample-size' but got '{result.entities.attribute_slug}'"
        )
        # tile_size should NOT be set (since sample_size was extracted first)
        assert result.entities.tile_size is None, (
            f"Expected tile_size to be None when sample_size is set, but got '{result.entities.tile_size}'"
        )

    def test_sample_size_with_space_12_x_24(self):
        """Test extraction of sample size with spaces '12 x 24'."""
        result = classify("I need a 12 x 24 sample")
        
        assert result.entities.sample_size is not None, (
            "Expected sample_size to be extracted from '12 x 24 sample'"
        )
        assert '12' in result.entities.sample_size and '24' in result.entities.sample_size
        assert result.entities.attribute_slug == "pa_sample-size"

    def test_sample_size_with_by_keyword(self):
        """Test extraction of sample size with 'by' keyword."""
        result = classify("get me a sample 6 by 6")
        
        assert result.entities.sample_size is not None, (
            "Expected sample_size to be extracted from 'sample 6 by 6'"
        )
        assert '6' in result.entities.sample_size
        assert result.entities.attribute_slug == "pa_sample-size"

    def test_small_sample_descriptive(self):
        """Test extraction of descriptive sample size 'small sample'.
        
        Note: This is an edge case - 'small sample' may match SAMPLE_REQUEST intent instead.
        The key requirement is that numeric sample sizes work correctly.
        """
        result = classify("I need a small sample")
        
        # Either sample_size is extracted OR intent is SAMPLE_REQUEST (both are acceptable)
        if result.entities.sample_size is not None:
            assert "small" in result.entities.sample_size.lower(), (
                f"Expected sample_size to contain 'small' but got '{result.entities.sample_size}'"
            )
            assert result.entities.attribute_slug == "pa_sample-size"
        else:
            # It's acceptable for this to match SAMPLE_REQUEST instead
            assert result.intent == Intent.SAMPLE_REQUEST, (
                f"Expected either sample_size to be set or SAMPLE_REQUEST intent, "
                f"but got intent={result.intent}"
            )

    def test_large_sample_descriptive(self):
        """Test extraction of descriptive sample size 'large sample'.
        
        Note: This is an edge case - 'large sample' may match SAMPLE_REQUEST intent instead.
        The key requirement is that numeric sample sizes work correctly.
        """
        result = classify("show me large sample")
        
        # Either sample_size is extracted OR intent is SAMPLE_REQUEST (both are acceptable)
        if result.entities.sample_size is not None:
            assert "large" in result.entities.sample_size.lower()
            assert result.entities.attribute_slug == "pa_sample-size"
        else:
            # It's acceptable for this to match SAMPLE_REQUEST instead
            assert result.intent == Intent.SAMPLE_REQUEST, (
                f"Expected either sample_size to be set or SAMPLE_REQUEST intent, "
                f"but got intent={result.intent}"
            )

    def test_extract_size_skips_when_sample_size_set(self):
        """Test that _extract_size() skips when sample_size is already populated."""
        result = classify("show me tiles of sample size 24x48")
        
        # sample_size should be set
        assert result.entities.sample_size is not None
        # tile_size should NOT be set (skipped because sample_size was set first)
        assert result.entities.tile_size is None, (
            f"Expected tile_size to be None when sample_size is set, "
            f"but got '{result.entities.tile_size}'"
        )
        # attribute_slug should point to sample-size, not tile-size
        assert result.entities.attribute_slug == "pa_sample-size", (
            f"Expected attribute_slug='pa_sample-size' but got '{result.entities.attribute_slug}'"
        )

    def test_tile_size_without_sample_keyword(self):
        """Test that tile size is extracted normally when 'sample' is not present."""
        result = classify("show me tiles of size 24x48")
        
        # tile_size should be set
        assert result.entities.tile_size is not None, (
            "Expected tile_size to be extracted from 'tiles of size 24x48'"
        )
        # sample_size should NOT be set (no 'sample' keyword)
        assert result.entities.sample_size is None, (
            f"Expected sample_size to be None when 'sample' keyword is absent, "
            f"but got '{result.entities.sample_size}'"
        )
        # attribute_slug should point to tile-size
        assert result.entities.attribute_slug == "pa_tile-size", (
            f"Expected attribute_slug='pa_tile-size' but got '{result.entities.attribute_slug}'"
        )

    def test_sample_size_intent_classification(self):
        """Test that sample size triggers FILTER_BY_SIZE intent."""
        result = classify("show me tiles of sample size 12x24")
        
        # Should classify as FILTER_BY_SIZE (or CATEGORY_BROWSE_FILTERED if category is also extracted)
        assert result.intent in [Intent.FILTER_BY_SIZE, Intent.CATEGORY_BROWSE_FILTERED], (
            f"Expected FILTER_BY_SIZE or CATEGORY_BROWSE_FILTERED but got {result.intent}"
        )
        # Should have high confidence
        assert result.confidence >= 0.90, (
            f"Expected confidence >= 0.90 but got {result.confidence}"
        )


class TestSampleSizeAPIBuilder:
    """Test that API builder correctly handles sample_size."""

    def test_api_builder_includes_sample_size_in_attr_map(self):
        """Test that api_builder can map pa_sample-size to entities.sample_size."""
        from models import ClassifiedResult, ExtractedEntities, Intent
        from api_builder import build_api_calls
        import json
        
        # Create a mock classification result with sample_size
        entities = ExtractedEntities()
        entities.sample_size = '12"x24"'
        entities.attribute_slug = "pa_sample-size"
        entities.attribute_term_ids = [123, 456]
        
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_SIZE,
            entities=entities,
            confidence=0.90
        )
        
        # Build API calls
        api_calls = build_api_calls(result)
        
        # Should have at least one API call
        assert len(api_calls) > 0, "Expected at least one API call"
        
        # Should use custom API with filters
        custom_call = None
        for call in api_calls:
            if call.is_custom_api and "products-by-attribute" in call.endpoint:
                custom_call = call
                break
        
        assert custom_call is not None, "Expected custom API call for attribute filtering"
        
        # Check that filters include pa_sample-size
        filters = json.loads(custom_call.params["filters"])
        assert filters[0]["attribute"] == "pa_sample-size", (
            f"Expected attribute='pa_sample-size' but got '{filters[0]['attribute']}'"
        )
        # Should have sample size value without quotes (stripped by lambda)
        assert "12x24" in filters[0]["terms"], (
            f"Expected terms to contain '12x24' but got '{filters[0]['terms']}'"
        )

    def test_api_builder_category_with_sample_size(self):
        """Test that category browse with sample_size uses CATEGORY_BROWSE_FILTERED."""
        from models import ClassifiedResult, ExtractedEntities, Intent
        from api_builder import build_api_calls
        import json
        
        # Create a classification with both category and sample_size
        entities = ExtractedEntities()
        entities.category_id = 42
        entities.category_name = "Tile"
        entities.sample_size = '12"x24"'
        entities.attribute_slug = "pa_sample-size"
        entities.attribute_term_ids = [789]
        
        result = ClassifiedResult(
            intent=Intent.CATEGORY_BROWSE_FILTERED,
            entities=entities,
            confidence=0.95
        )
        
        # Build API calls
        api_calls = build_api_calls(result)
        
        # Should have two API calls: category browse + attribute filter
        assert len(api_calls) >= 2, "Expected at least two API calls for category + attribute filter"
        
        # First call should be category browse
        first_call = api_calls[0]
        assert "category" in first_call.params, "Expected category param in first API call"
        assert first_call.params["category"] == "42"
        
        # Second call should use custom API with both filters
        second_call = api_calls[1]
        assert second_call.is_custom_api, "Second call should be custom API"
        assert "products-by-attribute" in second_call.endpoint
        
        filters = json.loads(second_call.params["filters"])
        # Should include both sample-size and category filters
        attr_slugs = [f["attribute"] for f in filters]
        assert "pa_sample-size" in attr_slugs, "Expected pa_sample-size in filters"
        assert "category" in attr_slugs, "Expected category in filters"
