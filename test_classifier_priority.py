"""
Tests for Classifier Priority Changes

Validates that classifier correctly prioritizes intents when both
category and attributes/product names are present.
"""

import pytest
from classifier import classify
from models import Intent


class TestClassifierPriority:
    """Test classifier priority when multiple entities are extracted."""

    def test_category_browse_filtered_intent(self):
        """Test that category + attribute triggers CATEGORY_BROWSE_FILTERED."""
        result = classify("show me tiles of size 12x24")
        
        # If category is extracted, should be CATEGORY_BROWSE_FILTERED
        # If category is not extracted, should be FILTER_BY_SIZE
        if result.entities.category_id is not None:
            assert result.intent == Intent.CATEGORY_BROWSE_FILTERED, (
                f"Expected CATEGORY_BROWSE_FILTERED when category and attribute are both extracted, "
                f"but got {result.intent}"
            )
            assert result.confidence >= 0.95, (
                f"Expected confidence >= 0.95 but got {result.confidence}"
            )
        else:
            # Fallback if category 'tiles' is not in store
            assert result.intent == Intent.FILTER_BY_SIZE, (
                f"Expected FILTER_BY_SIZE when only attribute is extracted, but got {result.intent}"
            )

    def test_category_with_finish_filtered(self):
        """Test that category + finish triggers CATEGORY_BROWSE_FILTERED."""
        result = classify("show me wall tiles with matte finish")
        
        # Should extract both category and finish
        if result.entities.category_id is not None and result.entities.finish:
            assert result.intent == Intent.CATEGORY_BROWSE_FILTERED, (
                f"Expected CATEGORY_BROWSE_FILTERED but got {result.intent}"
            )
            assert result.confidence >= 0.95

    def test_category_with_color_filtered(self):
        """Test that category + color triggers CATEGORY_BROWSE_FILTERED."""
        result = classify("show me tiles in gray tones")
        
        if result.entities.category_id is not None and result.entities.color_tone:
            assert result.intent == Intent.CATEGORY_BROWSE_FILTERED, (
                f"Expected CATEGORY_BROWSE_FILTERED but got {result.intent}"
            )

    def test_category_with_sample_size_filtered(self):
        """Test that category + sample size triggers CATEGORY_BROWSE_FILTERED."""
        result = classify("show me tiles of sample size 12x24")
        
        if result.entities.category_id is not None and result.entities.sample_size:
            assert result.intent == Intent.CATEGORY_BROWSE_FILTERED, (
                f"Expected CATEGORY_BROWSE_FILTERED when category and sample_size are extracted, "
                f"but got {result.intent}"
            )

    def test_product_search_in_category_intent(self):
        """Test that product name + category triggers PRODUCT_SEARCH_IN_CATEGORY."""
        result = classify("show me Carrara in wall tiles")
        
        # If both product and category are extracted
        if result.entities.product_name and result.entities.category_id is not None:
            assert result.intent == Intent.PRODUCT_SEARCH_IN_CATEGORY, (
                f"Expected PRODUCT_SEARCH_IN_CATEGORY when both product and category are extracted, "
                f"but got {result.intent}"
            )
            assert result.confidence >= 0.95, (
                f"Expected confidence >= 0.95 but got {result.confidence}"
            )

    def test_product_with_category_and_attributes(self):
        """Test that product name + category + attributes triggers PRODUCT_SEARCH_IN_CATEGORY."""
        result = classify("show me Carrara tiles in matte finish")
        
        # If product, category, and attributes are all extracted
        if (result.entities.product_name and 
            result.entities.category_id is not None and 
            result.entities.finish):
            assert result.intent == Intent.PRODUCT_SEARCH_IN_CATEGORY, (
                f"Expected PRODUCT_SEARCH_IN_CATEGORY but got {result.intent}"
            )
            # Should have highest confidence (0.96)
            assert result.confidence >= 0.96, (
                f"Expected confidence >= 0.96 but got {result.confidence}"
            )

    def test_category_only_triggers_category_browse(self):
        """Test that category alone (no attributes) triggers CATEGORY_BROWSE."""
        result = classify("show me wall tiles")
        
        # If only category is extracted (no attributes, no product name)
        if (result.entities.category_id is not None and 
            not result.entities.product_name and
            not any([result.entities.finish, result.entities.tile_size, 
                    result.entities.sample_size, result.entities.color_tone])):
            assert result.intent == Intent.CATEGORY_BROWSE, (
                f"Expected CATEGORY_BROWSE when only category is extracted (no attributes), "
                f"but got {result.intent}"
            )
            assert result.confidence == 0.94, (
                f"Expected confidence 0.94 but got {result.confidence}"
            )

    def test_attribute_only_without_category(self):
        """Test that attribute alone (no category) triggers attribute filter intent."""
        result = classify("show me matte finish")
        
        # Should classify as FILTER_BY_FINISH if no category extracted
        if not result.entities.category_id:
            assert result.intent == Intent.FILTER_BY_FINISH, (
                f"Expected FILTER_BY_FINISH when only finish is extracted (no category), "
                f"but got {result.intent}"
            )


class TestClassifierPriorityAPIBuilder:
    """Test that API builder correctly handles new intents."""

    def test_category_browse_filtered_api_calls(self):
        """Test API calls for CATEGORY_BROWSE_FILTERED intent."""
        from models import ClassifiedResult, ExtractedEntities, Intent
        from api_builder import build_api_calls
        import json
        
        entities = ExtractedEntities()
        entities.category_id = 123
        entities.category_name = "Test Category"
        entities.tile_size = "12x24"
        entities.attribute_slug = "pa_tile-size"
        entities.attribute_term_ids = [456]
        
        result = ClassifiedResult(
            intent=Intent.CATEGORY_BROWSE_FILTERED,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        # Should have at least 2 calls: category browse + attribute filter
        assert len(api_calls) >= 2, "Expected at least 2 API calls"
        
        # First call: category browse
        assert "category" in api_calls[0].params
        assert api_calls[0].params["category"] == "123"
        
        # Second call: custom API with filters
        assert api_calls[1].is_custom_api
        assert "products-by-attribute" in api_calls[1].endpoint
        
        filters = json.loads(api_calls[1].params["filters"])
        # Should include both attribute and category
        attr_slugs = [f["attribute"] for f in filters]
        assert "pa_tile-size" in attr_slugs
        assert "category" in attr_slugs

    def test_product_search_in_category_api_calls(self):
        """Test API calls for PRODUCT_SEARCH_IN_CATEGORY intent."""
        from models import ClassifiedResult, ExtractedEntities, Intent
        from api_builder import build_api_calls
        
        entities = ExtractedEntities()
        entities.product_name = "Carrara"
        entities.category_id = 42
        entities.category_name = "Tile"
        
        result = ClassifiedResult(
            intent=Intent.PRODUCT_SEARCH_IN_CATEGORY,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        # Should have at least 1 call with both search and category params
        assert len(api_calls) >= 1, "Expected at least 1 API call"
        
        first_call = api_calls[0]
        assert "search" in first_call.params
        assert first_call.params["search"] == "Carrara"
        assert "category" in first_call.params
        assert first_call.params["category"] == "42"

    def test_product_search_in_category_with_attributes(self):
        """Test API calls when product + category + attributes are present."""
        from models import ClassifiedResult, ExtractedEntities, Intent
        from api_builder import build_api_calls
        import json
        
        entities = ExtractedEntities()
        entities.product_name = "Carrara"
        entities.category_id = 42
        entities.category_name = "Tile"
        entities.finish = "Matte"
        entities.attribute_slug = "pa_finish"
        entities.attribute_term_ids = [789]
        
        result = ClassifiedResult(
            intent=Intent.PRODUCT_SEARCH_IN_CATEGORY,
            entities=entities,
            confidence=0.96
        )
        
        api_calls = build_api_calls(result)
        
        # Should have 2 calls: search in category + attribute filter
        assert len(api_calls) >= 2, "Expected at least 2 API calls"
        
        # First call: search with category
        assert "search" in api_calls[0].params
        assert "category" in api_calls[0].params
        
        # Second call: custom API with attribute + category filters
        assert api_calls[1].is_custom_api
        filters = json.loads(api_calls[1].params["filters"])
        attr_slugs = [f["attribute"] for f in filters]
        assert "pa_finish" in attr_slugs
        assert "category" in attr_slugs

    def test_existing_category_browse_still_works(self):
        """Test that existing CATEGORY_BROWSE tests still pass when no attributes are present."""
        from models import ClassifiedResult, ExtractedEntities, Intent
        from api_builder import build_api_calls
        
        entities = ExtractedEntities()
        entities.category_id = 123
        entities.category_name = "Wall Tile"
        # No attributes set
        
        result = ClassifiedResult(
            intent=Intent.CATEGORY_BROWSE,
            entities=entities,
            confidence=0.94
        )
        
        api_calls = build_api_calls(result)
        
        # Should have 1 call: category browse only
        assert len(api_calls) == 1, "Expected exactly 1 API call for plain category browse"
        
        # Should have category param
        assert "category" in api_calls[0].params
        assert api_calls[0].params["category"] == "123"
        
        # Should NOT have attribute filters
        assert not api_calls[0].is_custom_api


class TestResponseGeneratorIntents:
    """Test that response generator handles new intents."""

    def test_response_generator_category_browse_filtered(self):
        """Test response message for CATEGORY_BROWSE_FILTERED."""
        from models import Intent, ExtractedEntities
        from response_generator import generate_bot_message
        
        entities = ExtractedEntities()
        entities.category_name = "Tile"
        entities.tile_size = "12x24"
        
        products = [
            {"name": "Product 1", "price": 10.99},
            {"name": "Product 2", "price": 20.99},
        ]
        
        message = generate_bot_message(
            intent=Intent.CATEGORY_BROWSE_FILTERED,
            entities=entities,
            products=products,
            confidence=0.95
        )
        
        # Should mention category and filter
        assert "Tile" in message
        assert "12x24" in message or "filtered" in message.lower()

    def test_response_generator_product_search_in_category(self):
        """Test response message for PRODUCT_SEARCH_IN_CATEGORY."""
        from models import Intent, ExtractedEntities
        from response_generator import generate_bot_message
        
        entities = ExtractedEntities()
        entities.product_name = "Carrara"
        entities.category_name = "Tile"
        
        # Use multiple products to trigger the category-aware message
        products = [
            {"name": "Carrara Marble", "price": 15.99},
            {"name": "Carrara White", "price": 18.99},
        ]
        
        message = generate_bot_message(
            intent=Intent.PRODUCT_SEARCH_IN_CATEGORY,
            entities=entities,
            products=products,
            confidence=0.95
        )
        
        # Should mention product name and category
        assert "Carrara" in message
        assert "Tile" in message

    def test_intent_labels_include_new_intents(self):
        """Test that INTENT_LABELS includes new intents."""
        from models import Intent
        from response_generator import INTENT_LABELS
        
        assert Intent.CATEGORY_BROWSE_FILTERED in INTENT_LABELS, (
            "INTENT_LABELS should include CATEGORY_BROWSE_FILTERED"
        )
        assert Intent.PRODUCT_SEARCH_IN_CATEGORY in INTENT_LABELS, (
            "INTENT_LABELS should include PRODUCT_SEARCH_IN_CATEGORY"
        )
        
        # Should map to appropriate labels
        assert INTENT_LABELS[Intent.CATEGORY_BROWSE_FILTERED] == "category"
        assert INTENT_LABELS[Intent.PRODUCT_SEARCH_IN_CATEGORY] == "search"
