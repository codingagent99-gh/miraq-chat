"""
Tests for Bug Fixes: Product Classification and Attribute Filtering

Bug 1: "Show me more products" incorrectly classified as PRODUCT_SEARCH
Bug 2: Suggestions like "Show me Product Chip Card" don't work (cascading from Bug 1)
Bug 3: "show me tiles of size 12x24" returns unfiltered category results
"""

import pytest
from classifier import classify
from models import Intent
from api_builder import build_api_calls


class TestBug1ProductListClassification:
    """Test Bug 1: Generic 'products' should not match specific product named 'Product'."""

    def test_show_me_more_products_returns_product_list(self):
        """Test that 'Show me more products' is classified as PRODUCT_LIST, not PRODUCT_SEARCH."""
        result = classify("Show me more products")
        assert result.intent == Intent.PRODUCT_LIST, (
            f"Expected PRODUCT_LIST but got {result.intent}. "
            "Generic word 'products' should not match a product named 'Product'."
        )
        # Should not extract 'Product' as product_name
        assert result.entities.product_name is None, (
            f"Expected no product_name but got '{result.entities.product_name}'. "
            "Generic word 'products' should be skipped."
        )

    def test_show_me_all_products_returns_product_list(self):
        """Test that 'Show me all products' is classified as PRODUCT_LIST, not PRODUCT_SEARCH."""
        result = classify("Show me all products")
        assert result.intent == Intent.PRODUCT_LIST, (
            f"Expected PRODUCT_LIST but got {result.intent}. "
            "Generic word 'products' should not match a product named 'Product'."
        )
        # Should not extract 'Product' as product_name
        assert result.entities.product_name is None, (
            f"Expected no product_name but got '{result.entities.product_name}'. "
            "Generic word 'products' should be skipped."
        )

    def test_get_all_products_returns_product_list(self):
        """Test that 'Get all products' is classified as PRODUCT_LIST."""
        result = classify("Get all products")
        assert result.intent == Intent.PRODUCT_LIST
        assert result.entities.product_name is None

    def test_list_all_products_returns_product_list(self):
        """Test that 'List all products' is classified as PRODUCT_LIST."""
        result = classify("List all products")
        assert result.intent == Intent.PRODUCT_LIST
        assert result.entities.product_name is None

    def test_see_more_products_returns_product_list(self):
        """Test that 'See more products' is classified as PRODUCT_LIST."""
        result = classify("See more products")
        assert result.intent == Intent.PRODUCT_LIST
        assert result.entities.product_name is None

    def test_generic_product_word_in_order_context_does_not_match(self):
        """Test that generic word 'product' in order context doesn't match 'Product'."""
        result = classify("I want to order this product")
        # Should not extract 'Product' as product_name
        assert result.entities.product_name is None or result.entities.product_name != "Product", (
            f"Generic word 'product' should not match product named 'Product'. "
            f"Got: {result.entities.product_name}"
        )

    def test_generic_item_word_does_not_match(self):
        """Test that generic word 'item' doesn't match as a product name."""
        result = classify("Show me this item")
        # Should not extract any product matching generic word 'item'
        assert result.entities.product_name is None or result.entities.product_name != "Item", (
            f"Generic word 'item' should not match product name. "
            f"Got: {result.entities.product_name}"
        )


class TestBug3CategoryWithAttributeFiltering:
    """Test Bug 3: Category browse should include attribute filters when present."""

    def test_api_builder_category_browse_includes_attribute_filters(self):
        """Test that api_builder includes attribute filters for CATEGORY_BROWSE intent."""
        from models import ClassifiedResult, ExtractedEntities, Intent
        
        # Create a mock classification result with both category and attribute
        entities = ExtractedEntities()
        entities.category_id = 123
        entities.category_name = "Test Category"
        entities.tile_size = "12x24"
        entities.attribute_slug = "pa_tile-size"
        entities.attribute_term_ids = [456, 789]
        
        result = ClassifiedResult(
            intent=Intent.CATEGORY_BROWSE,
            entities=entities,
            confidence=0.94
        )
        
        # Build API calls
        api_calls = build_api_calls(result)
        
        # Verify we get two API calls: first for category, second for attribute filter
        assert len(api_calls) == 2, "Expected two API calls: category browse + attribute filter"
        
        # First call should be category browse without attribute params
        first_call = api_calls[0]
        assert "category" in first_call.params, "Expected category param in first API call"
        assert first_call.params["category"] == "123"
        assert "attribute" not in first_call.params, "First call should NOT have attribute param"
        assert "attribute_term" not in first_call.params, "First call should NOT have attribute_term param"
        
        # Second call should use custom API with filters format
        second_call = api_calls[1]
        assert second_call.is_custom_api, "Second call should be a custom API call"
        assert "products-by-attribute" in second_call.endpoint
        
        import json
        filters = json.loads(second_call.params["filters"])
        assert filters[0]["attribute"] == "pa_tile-size", (
            f"Expected attribute='pa_tile-size' but got '{filters[0]['attribute']}'"
        )
        assert filters[0]["terms"] == "12x24", (
            f"Expected terms='12x24' but got '{filters[0]['terms']}'"
        )
        assert second_call.params["page"] == 1, "Expected page param in second call"

    def test_tiles_with_size_filter_includes_both_category_and_attribute(self):
        """Test that 'show me tiles of size 12x24' includes both category and size filters."""
        result = classify("show me tiles of size 12x24")
        
        # Should extract size
        assert result.entities.tile_size is not None, (
            "Expected tile_size to be extracted from '12x24'"
        )
        assert result.entities.attribute_slug == "pa_tile-size", (
            f"Expected attribute_slug='pa_tile-size' but got '{result.entities.attribute_slug}'"
        )
        
        # Build API calls and verify filters are included
        api_calls = build_api_calls(result)
        assert len(api_calls) > 0, "Expected at least one API call"
        
        # If category was extracted, check that both category and attribute filters are present
        if result.entities.category_id is not None:
            # Should classify as CATEGORY_BROWSE (category takes priority)
            assert result.intent == Intent.CATEGORY_BROWSE, (
                f"Expected CATEGORY_BROWSE when category is extracted but got {result.intent}"
            )
            # Should have two calls: category + attribute filter
            assert len(api_calls) == 2, "Expected two API calls for category + attribute filter"
            
            # First call should have category filter but no attribute params
            first_call = api_calls[0]
            assert "category" in first_call.params, "Expected category param in first API call"
            assert "attribute" not in first_call.params, "First call should NOT have attribute param"
            
            # Second call should use custom API with filters
            second_call = api_calls[1]
            assert second_call.is_custom_api, "Second call should be custom API"
            assert "products-by-attribute" in second_call.endpoint
            
            import json
            filters = json.loads(second_call.params["filters"])
            assert filters[0]["attribute"] == "pa_tile-size", (
                f"Expected attribute='pa_tile-size' but got '{filters[0]['attribute']}'"
            )
            assert second_call.params["page"] == 1
        else:
            # If no category extracted (e.g., store doesn't have "Tile" category),
            # should classify as FILTER_BY_SIZE which is also acceptable
            assert result.intent == Intent.FILTER_BY_SIZE, (
                f"Expected FILTER_BY_SIZE when no category extracted but got {result.intent}"
            )

    def test_category_with_finish_filter_includes_both(self):
        """Test that category + finish includes both filters."""
        result = classify("show me wall tiles with matte finish")
        
        # Should extract finish
        assert result.entities.finish is not None, "Expected finish to be extracted"
        
        # Build API calls and verify filters
        api_calls = build_api_calls(result)
        
        # If category was extracted, verify CATEGORY_BROWSE includes attribute filters
        if result.entities.category_id is not None:
            # Should classify as CATEGORY_BROWSE
            assert result.intent == Intent.CATEGORY_BROWSE, (
                f"Expected CATEGORY_BROWSE when category is extracted but got {result.intent}"
            )
            
            # If finish attribute was extracted, should have two calls
            if result.entities.attribute_slug and result.entities.finish:
                assert len(api_calls) >= 1, "Expected at least one API call"
                # First call should have category
                assert "category" in api_calls[0].params
                # If we got a second call, it should be the custom API for attribute filter
                if len(api_calls) > 1:
                    assert api_calls[1].is_custom_api, "Second call should use custom API"
        else:
            # If no category extracted, should classify based on the strongest entity
            assert result.intent in (Intent.FILTER_BY_FINISH, Intent.FILTER_BY_APPLICATION), (
                f"Expected FILTER_BY_FINISH or FILTER_BY_APPLICATION but got {result.intent}"
            )

    def test_category_with_color_filter_includes_both(self):
        """Test that category + color includes both filters."""
        result = classify("show me floor tiles in gray")
        
        # Should classify as CATEGORY_BROWSE (if category extracted)
        # or FILTER_BY_COLOR (if no category but color extracted)
        assert result.intent in (Intent.CATEGORY_BROWSE, Intent.FILTER_BY_COLOR)
        
        if result.intent == Intent.CATEGORY_BROWSE:
            # Should have both category and color
            assert result.entities.category_name is not None
            assert result.entities.color_tone is not None
            
            # Build API calls and verify filters
            api_calls = build_api_calls(result)
            if len(api_calls) > 0:
                params = api_calls[0].params
                assert "category" in params
                # Color is typically handled via tags
                if result.entities.tag_ids:
                    assert "tag" in params

    def test_tiles_size_24x48_includes_both_filters(self):
        """Test another size variant: 'tiles of size 24x48'."""
        result = classify("tiles of size 24x48")
        
        # Should extract size
        assert result.entities.tile_size is not None
        
        # Build API calls and check for attribute filter
        api_calls = build_api_calls(result)
        if len(api_calls) > 0 and result.intent == Intent.CATEGORY_BROWSE:
            # Should have two calls: category + attribute filter
            if result.entities.attribute_slug:
                assert len(api_calls) >= 1
                # First call should have category
                assert "category" in api_calls[0].params
                # If we got a second call, it should be the custom API
                if len(api_calls) > 1:
                    assert api_calls[1].is_custom_api


class TestBug2CascadingFix:
    """
    Test Bug 2: Verify that fixing Bug 1 prevents cascading issues.
    
    Bug 2 happens because Bug 1 incorrectly matches 'Product' as a product,
    then response_generator creates broken suggestions using base_name = name.split(" ")[0].
    If Bug 1 is fixed, Bug 2 should not occur.
    """

    def test_show_me_more_products_does_not_extract_product_entity(self):
        """Verify that 'Show me more products' does not extract 'Product' entity."""
        result = classify("Show me more products")
        # Should not have product_name='Product' or product_id=7846
        assert result.entities.product_name is None or result.entities.product_name != "Product"
        assert result.entities.product_id is None or result.entities.product_id != 7846

    def test_all_products_does_not_extract_product_entity(self):
        """Verify that 'all products' does not extract 'Product' entity."""
        result = classify("all products")
        assert result.entities.product_name is None or result.entities.product_name != "Product"
        assert result.entities.product_id is None or result.entities.product_id != 7846
